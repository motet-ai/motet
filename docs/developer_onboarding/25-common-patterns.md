# Common Patterns

This section covers reusable patterns for common scenarios in Motet development. These patterns have been proven in production and can be adapted for your use cases.

## Data Processing Pipeline

**Pattern**: Extract → Transform → Load workflow.

**Use Case**: ETL operations, data migration, batch processing.

```python
@motet.command()
def etl_pipeline(data: ETLData, motet: MotetContext) -> Dict[str, Any]:
    """ETL pipeline pattern."""
    from motet_sdk import CommandExecutionError
    
    try:
        # Extract
        extracted = motet.do(extract_data, data=ExtractData(source=data.source))
        
        # Transform
        transformed = motet.do(
            transform_data,
            data=TransformData(input_data=extracted)
        )
        
        # Load
        loaded = motet.do(
            load_data,
            data=LoadData(data=transformed["result"], destination=data.destination)
        )
        
        return {"status": "complete", "loaded": loaded}
    except CommandExecutionError as e:
        logger.error("ETL pipeline failed", error=str(e))
        raise
```

Each stage is its own command, so a failing transform does not re-run the extract and each stage can be tested alone. Note that this chain is sequential — three commands, three hops. If the stages do not depend on each other, `motet.join` runs them at once. Reach for a `Workflow` instead when you want per-step visibility or the ability to resume a failed run.

## Multi-Step Analysis Workflow

**Pattern**: Extract → Analyze → Summarize → Store.

**Use Case**: Document analysis, content processing, research workflows.

```python
from motet.core.workflow import Workflow, WorkflowStep

analysis_workflow = Workflow(
    workflow_id="analysis_workflow",
    name="Multi-Step Analysis",
    description="Extract, analyze, summarize, and store content",
    steps={
        "extract": WorkflowStep(
            step_id="extract",
            name="Extract",
            command_type="core.tool_execution",
            command_data={"tool_name": "core.file_read", "parameters": {"path": "{{file_path}}"}}
        ),
        "analyze": WorkflowStep(
            step_id="analyze",
            name="Analyze",
            command_type="core.agent_loop",
            command_data={"input": "Analyze: {{extract.content}}"},
            dependencies=["extract"]
        ),
        "summarize": WorkflowStep(
            step_id="summarize",
            name="Summarize",
            command_type="core.agent_loop",
            command_data={
                "input": "Summarize and analyze: {{extract.content}}"
            },
            dependencies=["extract"]
        ),
        "store": WorkflowStep(
            step_id="store",
            name="Store",
            command_type="core.tool_execution",
            command_data={
                "tool_name": "core.memory_store",
                "parameters": {"content": "{{summarize.result}}", "tags": ["analysis", "{{document_type}}"]}
            },
            dependencies=["summarize"]
        )
    }
)
```

`analyze` and `summarize` both depend only on `extract`, so they run in parallel with no extra arrangement. That is the main reason to declare dependencies rather than chain `motet.do` calls: the parallelism falls out of the graph instead of being something you maintain by hand.

## Tool Coordination Pattern

**Pattern**: Coordinate multiple tools in sequence.

**Use Case**: Web automation, API orchestration, multi-tool workflows.

```python
tool_coordination_workflow = Workflow(
    workflow_id="tool_coordination",
    name="Tool Coordination Workflow",
    description="Coordinate multiple tools in sequence",
    steps={
        "search": WorkflowStep(
            step_id="search",
            name="Search",
            command_type="core.tool_execution",
            command_data={"tool_name": "core.web_search", "parameters": {"query": "{{query}}"}}
        ),
        "navigate": WorkflowStep(
            step_id="navigate",
            name="Navigate",
            command_type="core.tool_execution",
            command_data={
                "tool_name": "mcp.playwright.browser_navigate",
                "parameters": {"url": "{{search.url}}"}
            },
            dependencies=["search"]
        ),
        "screenshot": WorkflowStep(
            step_id="screenshot",
            name="Screenshot",
            command_type="core.tool_execution",
            command_data={"tool_name": "mcp.playwright.browser_take_screenshot", "parameters": {}},
            dependencies=["navigate"]
        ),
        "extract": WorkflowStep(
            step_id="extract",
            name="Extract",
            command_type="core.tool_execution",
            command_data={"tool_name": "mcp.playwright.browser_snapshot", "parameters": {}},
            dependencies=["navigate"]
        )
    }
)
```

Browser steps share session state and only make sense in order, so the dependencies here are load-bearing rather than decorative. Use this shape when the tools must run against the same live session; independent tool calls belong in a fan-out instead.

## Error Recovery Pattern

**Pattern**: Retry with fallback.

**Use Case**: Resilient operations, graceful degradation.

```python
@motet.command()
def retry_with_fallback(data: RetryData, motet: MotetContext) -> Dict[str, Any]:
    """Retry with fallback pattern."""
    from motet_sdk import CommandExecutionError
    
    # Try primary operation
    result, error = motet.maybe(primary_operation, data=PrimaryData(...))
    
    if error:
        logger.warning(
            "Primary operation failed, using fallback",
            error=error.get("message")
        )
        # Fallback to secondary operation
        try:
            result = motet.do(fallback_operation, data=FallbackData(...))
        except CommandExecutionError as e:
            logger.error("Fallback also failed", error=str(e))
            raise
    
    return {"result": result}
```

`motet.maybe` returns a `(data, error)` tuple instead of raising, which is what keeps the fallback readable. Use it when failure is an expected branch. When failure is genuinely exceptional, let `motet.do` raise — wrapping everything in `maybe` turns real bugs into silent fallbacks.

## Fan-Out Fan-In Pattern

**Pattern**: Process multiple items in parallel, then aggregate.

**Use Case**: Batch processing, parallel analysis, data aggregation.

```python
@motet.command()
def fan_out_fan_in(data: FanData, motet: MotetContext) -> Dict[str, Any]:
    """Fan-out fan-in pattern."""
    from motet_sdk import ApplyExecutionError
    
    try:
        # Fan-out: Process multiple items in parallel
        results = motet.apply(
            process_item,
            inputs=data.items,
            batch_size=10  # Limit concurrency
        )
        
        # Fan-in: Aggregate results
        aggregated = motet.do(
            aggregate_results,
            data=AggregateData(results=results)
        )
        
        return {"aggregated": aggregated, "item_count": len(results)}
    except ApplyExecutionError as e:
        logger.error("Fan-out fan-in failed", error=str(e))
        raise
```

`batch_size` caps how many run at once, which matters when the fan-out is wide enough to starve the worker pool or trip a provider rate limit. `motet.apply` tolerates partial failure and returns the successes; if you need all-or-nothing, use `motet.join`.

## Caching and Optimization Pattern

**Pattern**: Cache results to avoid redundant computation.

**Use Case**: Expensive operations, repeated queries, performance optimization.

```python
@motet.command()
def cached_operation(data: CacheData, motet: MotetContext) -> Dict[str, Any]:
    """Caching pattern."""
    # Try to get from cache
    cached, error = motet.maybe(
        get_cache,
        data=CacheData(key=data.cache_key)
    )
    
    if not error and cached:
        logger.info("Cache hit", key=data.cache_key)
        return {"result": cached, "from_cache": True}
    
    # Cache miss - compute fresh
    logger.info("Cache miss, computing", key=data.cache_key)
    result = motet.do(expensive_operation, data=ExpensiveData(...))
    
    # Store in cache
    motet.do(
        store_cache,
        data=StoreCacheData(key=data.cache_key, value=result)
    )
    
    return {"result": result, "from_cache": False}
```

The lookup uses `maybe` because a miss is a normal outcome rather than an error. Be aware that the read and the write are not atomic: two callers can miss simultaneously and both compute. That is usually acceptable for a cache, and not acceptable if the computation has side effects.

## State Management Pattern

**Pattern**: Manage state across multiple operations.

**Use Case**: Multi-step processes, stateful workflows, context preservation.

```python
@motet.command()
def stateful_workflow(data: StatefulData, motet: MotetContext) -> Dict[str, Any]:
    """State management pattern."""
    # Initialize state
    state = {
        "step": "initialized",
        "data": data.initial_data,
        "metadata": {}
    }
    
    # Store initial state
    motet.memory.store(
        content=str(state),
        tags=["workflow_state", data.workflow_id]
    )
    
    # Step 1: Process with state
    result1 = motet.do(
        step1,
        data=Step1Data(input=data.input, state=state)
    )
    state["step"] = "step1_complete"
    state["data"] = result1
    
    # Step 2: Process with updated state
    result2 = motet.do(
        step2,
        data=Step2Data(input=result1, state=state)
    )
    state["step"] = "step2_complete"
    state["data"] = result2
    
    # Final state
    motet.memory.store(
        content=str(state),
        tags=["workflow_state", data.workflow_id, "final"]
    )
    
    return {"final_state": state}
```

Writing state at the start and the end leaves a record of how far a run got. Note that the intermediate state lives in a local variable, so it dies with the command — if you need to resume from the middle rather than just diagnose afterwards, persist each transition instead of only the endpoints.

## Conditional Execution Pattern

**Pattern**: Execute steps conditionally based on results.

**Use Case**: Conditional workflows, branching logic, adaptive processes.

```python
conditional_workflow = Workflow(
    workflow_id="conditional",
    name="Conditional Workflow",
    steps={
        "check": WorkflowStep(
            step_id="check",
            name="Check",
            command_type="validation",
            command_data={"input": "{{input}}"}
        ),
        "if_valid": WorkflowStep(
            step_id="if_valid",
            name="If Valid",
            command_type="process",
            command_data={"input": "{{check.result}}"},
            dependencies=["check"],
            skip_condition="if_equals:check.valid:False"  # Skip if not valid
        ),
        "if_invalid": WorkflowStep(
            step_id="if_invalid",
            name="If Invalid",
            command_type="error_handler",
            command_data={"error": "{{check.error}}"},
            dependencies=["check"],
            skip_condition="if_equals:check.valid:True"  # Skip if valid
        )
    }
)
```

Both branches depend on the same step and carry opposite skip conditions, which is how you express if/else in a dependency graph. `skip_condition` has no numeric comparison, so have `check` emit a boolean rather than trying to branch on a score directly.

## Parallel Exploration Pattern

**Pattern**: Explore multiple paths in parallel, then select best.

**Use Case**: Optimization, search, exploration, A/B testing.

```python
@motet.command()
def parallel_exploration(data: ExplorationData, motet: MotetContext) -> Dict[str, Any]:
    """Parallel exploration pattern."""
    from motet_sdk import GatherExecutionError
    
    try:
        # Explore multiple paths in parallel
        paths = motet.join([
            (explore_path_a, PathData(strategy="strategy_a", ...)),
            (explore_path_b, PathData(strategy="strategy_b", ...)),
            (explore_path_c, PathData(strategy="strategy_c", ...))
        ])
        
        # Evaluate and select best
        best_path = motet.do(
            select_best,
            data=SelectBestData(paths=paths, criteria=data.criteria)
        )
        
        return {"best_path": best_path, "all_paths": paths}
    except GatherExecutionError as e:
        logger.error("Parallel exploration failed", error=str(e))
        raise
```

`motet.join` fails if any branch fails, which is the behavior you want when the comparison is only meaningful with every path present. If a missing path is survivable, use `motet.apply` or wrap each branch in `maybe` instead.

## Workflow Composition Pattern

**Pattern**: Compose workflows from other workflows.

**Use Case**: Complex processes, reusable workflows, hierarchical workflows.

```python
parent_workflow = Workflow(
    workflow_id="parent",
    name="Parent Workflow",
    steps={
        "preparation": WorkflowStep(
            step_id="preparation",
            name="Preparation",
            command_type="workflow_execution",
            command_data={"workflow_id": "preparation_workflow", ...}
        ),
        "main_processing": WorkflowStep(
            step_id="main_processing",
            name="Main Processing",
            command_type="workflow_execution",
            command_data={"workflow_id": "main_workflow", ...},
            dependencies=["preparation"]
        ),
        "cleanup": WorkflowStep(
            step_id="cleanup",
            name="Cleanup",
            command_type="workflow_execution",
            command_data={"workflow_id": "cleanup_workflow", ...},
            dependencies=["main_processing"]
        )
    }
)
```

Each child is a workflow you can run and test on its own, which is the practical payoff: a parent composing three tested workflows is easier to reason about than one twelve-step graph.

## Choosing between them

Two questions settle most cases.

**Code or a declared workflow?** Compose in code with `motet.do` and its siblings when the logic is yours and the shape is simple — it reads better and debugs more easily. Declare a `Workflow` when you want per-step visibility, resumability, conditional skipping, or when you want the model to be able to call the whole thing as a single `workflow_*` tool.

**How much failure can you absorb?** This is the difference between the composition helpers, and picking by it is better than wrapping everything in try/except:

| If you need | Use |
|---|---|
| One command's result | `motet.do` — raises on failure |
| Several different commands at once, all required | `motet.join` — raises if any branch fails |
| One command over many inputs, partial results usable | `motet.apply` — returns the successes |
| A fallback when something fails | `motet.maybe` — returns `(data, error)`, never raises |
| Steps someone else can watch, resume, or skip | `Workflow` |
| To start work you will not wait for | `motet.dispatch` |

## Next Steps

- **[Example Bundles](./26-example-bundles.md)** - See complete bundles
- **[Best Practices](./27-best-practices.md)** - Learn from experience
- **[Troubleshooting Guide](./30-troubleshooting-guide.md)** - Solve problems

## Navigation

- **[← Back to Documentation Home](./00-landing-page.md)** - Main documentation hub

---

**Last Updated**: 2026-08-24
