## Package: registry

**Unified scoped registry primitives** — grants (**`ScopeGrant`**), hierarchical scope (**`RegistryScope`**), filters (**`ScopeFilter`**), and a generic **`ScopedRegistry`** synchronized with **`WorkerLock`** so tools, workflows, agents, and commands share one namespacing story.

### Purpose

- **Consistent tenancy / role visibility**: Entries carry explicit grants checked by filters instead of ad hoc string prefixes everywhere.
- **Pool-safe mutations**: Registry methods synchronize with concurrency primitives suited to Celery pool types (companions).
- **Qualified IDs**: Parsing and normalization helpers keep `{namespace}.{resource}` semantics aligned across registrars.

### Core components

#### `base.py`

**`ScopedRegistry`**, **`RegistryScope`**, **`ScopeGrant`**, **`ScopeFilter`**, **`RegistryEntry`**, and related protocols shared by specialized registries.

#### `naming.py`

Helpers such as **`CORE_NAMESPACE`** and **`normalize_namespace`** plus qualified-ID parsing utilities used when deriving scope from registrar keys.

### Notes

Prefer **`from motet.core.registry import...`** (see **`__init__.py`**) so consumers pick up the curated public surface as symbols move between modules.
