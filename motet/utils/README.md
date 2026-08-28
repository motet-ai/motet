## Package: motet.utils

**Compatibility import path** for a tiny set of helpers implemented in **`motet.core.utils`**. External MCP servers and out-of-tree callers can depend on **`from motet.utils import ...`** without importing deep runtime modules.

### Purpose

- Preserve **stable top-level imports** when code predates the **`motet.core.utils`** layout.
- Avoid duplicating **`run_async_safe`** (and future re-exports, if any) in multiple packages.

### Current exports

Declared in **`motet/utils/__init__.py`** — primarily **`run_async_safe`** from **`motet.core.utils.async_helpers`**.

### Notes

**First-party Motet code** should import from **`motet.core.utils`** unless the file is explicitly bridging external expectations.

### Related

- Implementation: **`motet/core/utils/README.md`**
