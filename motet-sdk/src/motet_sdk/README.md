## Package: motet_sdk

**Bundle-author surface** for Motet: protocol types, command decorator, manifest validation, concurrency helpers, and CLI — without importing the **`motet`** runtime. At deploy time the runtime injects real implementations via the bundle bridge.

### Purpose

- **`distributed_command` / `motet`**: Register commands and tools that execute on workers when the bundle loads.
- **`MotetContext`**: Typed access to tools, memory (`motet.do`, `motet.join`, etc. as documented in SDK), vault, and composition helpers the protocol exposes.
- **`MockMotetContext`**: Unit-test doubles that avoid a live stack.
- **`load_manifest` / `validate_manifest`**: Authoring-time validation of **`manifest.yaml`**.
- **Concurrency**: Pool-agnostic primitives and **`run_async_safe`** (stdlib fallbacks; runtime replaces with worker-aware implementations).

### Core layout

| Module / path | Role |
|---------------|------|
| **`__init__.py`** | Curated public exports (see **`__all__`**) |
| **`context.py`** | **`MotetContext`** protocol surface |
| **`command.py`** | **`distributed_command`**, identity helpers |
| **`capabilities.py`** | **`WorkerCapability`** enum for command requirements |
| **`concurrency.py`** | Lock/sleep/executor/thread helpers + **`run_async_safe`** |
| **`models.py`** | Shared Pydantic/command metadata types |
| **`manifest.py`** | **`BundleManifest`**, load/validate |
| **`motet_namespace.py`** | **`motet`** namespace decorator object |
| **`preparation.py`** | Bundle-facing artifact preparation manifest types |
| **`testing.py`** | **`MockMotetContext`** |
| **`cli/`** | Full **`motet-cli`** implementation — see **`cli/README.md`** |
| **`docker/`** | Compose assets used by **`motet-cli local`** — see **`docker/README.md`** |

### Related

- Published package overview: **`motet-sdk/README.md`** (repository root of this package)
- Product-facing SDK narrative: **`docs/developer_onboarding/38-sdk-reference.md`** (full Motet repo)
