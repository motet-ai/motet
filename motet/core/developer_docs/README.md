## Package: developer_docs

Product-owned developer onboarding corpus. Same filesystem root as
`GET /api/v1/developer-docs` (`docs/developer_onboarding/`, override
`MOTET_DEVELOPER_DOCS_DIR`).

### Purpose

- Give the HTTP docs API and `core.docs_read` one path-resolution and id-safety
  implementation.
- Expose a **curated agent-facing allowlist** (workflow-authoring pack first)
  so agents can read how-to prose without indexing docs as tenant artifacts.
- Lexical search (`search_docs` / `GET /api/v1/developer-docs/search`) matches
  title and body for the human docs rail.

### What this is not

- Not artifact RAG. Do not merge this corpus into the artifact store.
- Not hybrid ranking. HTTP search is substring/token AND over title and body.
  Agents use known-id `core.docs_read` with an optional heading slice.

### Layout

- `allowlist.py` — agent-facing catalog (`11-workflow-system`, `17-building-workflows`)
- `corpus.py` — docs root, list/read, section windows, char windows
- `taxonomy.py` — exclusive nav groups for the human HTTP list (Home, Start, Concepts, Build, Runtime, State, Operate, Surfaces, Guides). Home is the landing page.
- `search.py` — lexical title/heading/body search for the HTTP search endpoint
- Built-in tool: `motet/core/tools/builtin/docs_read.py` (`core.docs_read`)
- HTTP: `motet/interfaces/api/v1/developer_docs.py` (full numbered corpus, grouped by taxonomy, plus product version; `/search` is registered before `/{doc_id}`)

### Usage

```python
from motet.core.developer_docs import read_agent_facing, list_all_docs, group_docs, search_docs

catalog = read_agent_facing()
page = read_agent_facing(doc_id="11-workflow-system", section="YAML structure")
humans = group_docs(list_all_docs())
hits = search_docs("worker targeting")
```

Workers need the markdown in the image (or a bind mount). The worker runtime
Dockerfile copies `docs/developer_onboarding` next to Motet source, matching
the API image.
