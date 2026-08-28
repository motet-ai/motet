# core.agents — Agent Configuration Registry

Agent configuration and registry. Agent-related code lives under `core/agents` so the registry, resolution helpers, and built-in configs stay in one place.

## Contents

- **registry.py**: `AgentConfig`, `ToolFilter`, `TurnHooks`, `AgentConfigRegistry`, plus `resolve_tools`, `resolve_agent_id`, `ensure_conversation_id_prefix`, and built-in configs for `core.default` and `core.motet_admin`. Lookup from fully-qualified `agent_id` to config used when invoking `agent_turn`. Bare chat names are opt-in via `AgentConfig.aliases` only (not auto-claimed from `agent_id`).
- **discovery.py**: `serialize_agent_config` / `list_visible_agents` for API and ops-dashboard. Serialization includes model overrides, loop limits, skills, metadata, surfaces, tool filter, turn hooks, output contract, and handoffs.
- **prompt_policy.py**: Turn prompt-assembly policies from `AgentConfig.metadata.prompt_policy` (default motet_system_primary vs `client_system_primary` for OpenAI-compat / Cursor backends).

## Usage

```python
from motet.core.agents import get_agent_registry

registry = get_agent_registry
config = registry.get("core.motet_admin")
# config.system_prompt, config.tool_filter, config.turn_hooks, etc.
```

## Shortlist shape in discovery mode

**Meta-tool progressive disclosure** is the only discovery path. The tools segment of the provider cache prefix stays a small stable bag:

- Resident: `core.help`, `core.tools_search`, `core.tool_call`, `core.spawn_agents` (plus `required_tools` / keyword pins).
- `core.handoff` is in the catalog like `core.spawn_agents`. It is pinned when `AgentConfig.handoffs` is non-empty and depth is under 2; it is not always-sticky. The `handoffs` list is the grant.
- Catalog reachability: `core.tools_search` → `core.tool_call` (schemas in the observation tail).
- No per-turn embedding shortlist prelude — `FunctionDiscoveryVectorStore` is used on demand by `tools_search`.

Size `max_tools` above always-sticky members (4) plus the largest keyword pin group (4) — `merge_sticky_tool_names` truncates *after* admitting pins, so a tight `max_tools` silently drops them.

## Related

- **core/reasoning/react/**: `agent` command and `AgentData` (execution); this package holds *configuration* for that command.
