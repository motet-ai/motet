# Bundle Examples

This directory contains SDK-oriented bundle examples:

- `hello-world`: minimal command + tool bundle (see `hello-world/README.md`)
- `celebs`: config-focused bundle with persona agents (see `celebs/README.md`)
- `get-news`: multi-step news aggregation workflow using browser-capable tooling
- `deep-research`: LLM planning, parallel fan-out, memory persistence, custom tools
- `content-review`: multi-perspective review with `motet.join`, `motet.maybe`, and sequential `motet.do` chains
- `expert-panel`, `background-thinker`: additional multi-agent / long-running patterns — see each bundle’s `README.md`
- `roundtable`: facilitator pattern — an agent picks who speaks next at runtime via `agents.turn`, runs rounds through the agentic loop, and synthesizes from a shared transcript; the dynamic counterpart to `expert-panel`'s declared workflow. See `roundtable/README.md`
- `app-builder`: self-hosted development agent (pick issue → implement → test gate → PR) with product compose/CLI under `deploy/` + `cli/` — see `app-builder/README.md` and `install.sh`
- `cursor`: OpenAI-compatible facade showcase — Cursor / IDE backend agent (`cursor.backend`) with client harness primary + Motet tools / handback; see `cursor/README.md`
- `openai-gateway`: OpenAI-compatible **passthrough** cookbook — Motet as a multi-provider drop-in gateway (no agents/tools; facade env + service account); see `openai-gateway/README.md`
- `langfuse-cms`: Langfuse Cloud prompt fetch/update for one agent + optional generation cost push (YAML fallback); see `langfuse-cms/README.md`
- `plan-mode`: inspectable planning — one agent writes structured plan/todos (JSON + markdown) as draft, `approve_plan` gate, then implements with todo progress; see `plan-mode/README.md`
- `skills-demo`: Agent Skills (Apache Anthropic skills + Motet runner); optional document skills via local fetch (gitignored); see `skills-demo/README.md`

Test fixtures that are intentionally test-specific (for example, failing lint
fixtures) remain under `tests/bundles` in the main Motet repo.
