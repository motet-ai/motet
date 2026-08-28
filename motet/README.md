## Runtime tree: motet

**Motet runtime Python package** — orchestration, workers, tools, interfaces, and related code that executes in the FastAPI process and Celery workers (contrasts with **`motet-sdk/`**, which is the bundle-author contract only).

### Purpose

- Ship the **domain + infrastructure** layers other packages import as **`motet.*`** during production runs.
- Isolate **HTTP/CLI/UI adapters** under **`interfaces/`** and **`cli/`** from **`core/`** business logic.
- Expose a **narrow stable import** surface for external helpers via **`motet.utils`** without bloating the SDK.

### Layout

| Path | Role |
|------|------|
| **`core/`** | Commands, reasoning, tools, memory, models, RAG, workers, distributed state, registry, execution, etc. |
| **`interfaces/`** | REST API (`api/v1`), frontend workspace, templates, session plumbing |
| **`cli/`** | `motet-cli` entrypoints and subcommands |
| **`utils/`** | Compat re-exports (see **`motet/utils/README.md`**) |
| **`services/`** | Reserved ancillary layouts (currently minimal; see folder README) |

Subpackages under **`core/`** and **`interfaces/`** usually include their own **`README.md`** with **`## Package:`** style documentation.

### Related

- Repository overview: **`README.md`** (repo root)
- Agent / contributor rules: **`AGENTS.md`**
- Bundle SDK: **`motet-sdk/`**
