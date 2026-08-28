## Package: motet.services (directory)

**Reserved filesystem layout** for ancillary service assets. This tree is **not** the primary home of production domain code — most services live under **`motet/core/`** (for example **`motet.core.services`**, **`motet.core.embedding`**) or deployment repos.

### Purpose

- Hold **optional or experimental** service folders (for example embedding-server scaffolding) without mixing them into **`core/`** until they graduate.

### Notes

Operational embedding stacks are documented beside **Compose** definitions and **`motet/core/embedding/`**. Extend this README when Python modules land here permanently.
