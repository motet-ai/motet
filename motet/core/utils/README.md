## Package: core.utils

**Small cross-cutting runtime helpers.** Intentionally minimal—heavy logic stays in owning domains (workers, orchestration, tools).

### Purpose

- **Async bridging in sync workers**: Run coroutines safely from synchronous Celery-compatible code paths without assuming a particular pool type.

### Core components

#### `async_helpers.py`

**`run_async_safe`** executes coroutines on an appropriate loop or thread offload depending on **`gevent` / `eventlet` / threaded** contexts (companion).

### Usage

```python
from motet.core.utils.async_helpers import run_async_safe

result = run_async_safe(some_async_function, timeout=30.0)
```

### Notes

**`motet.utils`** (top-level package) **re-exports** selected symbols (**`run_async_safe`**) for external MCP servers and existing import paths (**`from motet.utils import run_async_safe`**). **Inside Motet**, prefer **`motet.core.utils`** so dependency direction stays clear.
