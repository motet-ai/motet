# Bundle Scoping and Visibility

This guide explains how bundle contents are scoped, who can see them, and what must be true for them to execute successfully.

## Why this matters

A bundle can be deployed successfully and still appear different to different callers:

- One caller can discover a command/tool/workflow/agent
- Another caller cannot see it
- A caller can discover it but execution fails due to routing/capability mismatch

Understanding visibility and scope prevents these issues.

## Scope model in practice

Motet applies visibility using request context and registry metadata.

Inputs that commonly affect visibility:
- tenant
- motet
- principal/user identity
- roles

Artifact types affected:
- commands
- tools
- workflows
- agents

## Bundle naming and namespacing

Bundle artifacts should always be namespaced with the bundle name from `manifest.yaml`.

Examples:
- command type: `my-bundle.hello`
- tool name: `my-bundle.lookup_customer`
- workflow id: `my-bundle.enrich_account`
- agent id: `my-bundle.research_agent`

Why:
- prevents collisions across bundles
- makes unload/prune behavior deterministic
- keeps discovery and execution aligned

## What bundles contain

Typical bundle structure:
- `manifest.yaml` (name/version/description)
- `commands/` (distributed commands)
- `tools/` (tool functions/registrations)
- `workflows/` (workflow YAML definitions)
- `agents/agents.yaml` (agent configurations; see [Agent Loop – Configuring agents in a bundle (YAML)](./07a-agent-loop.md#configuring-agents-in-a-bundle-yaml))
- `config/` (routing/model/MCP config)

## Visibility vs executability

Discovery visibility and execution routing are related but distinct.

- **Visible** means the caller can see the item in discovery/list APIs
- **Executable** means there is a suitable worker/path that can run it in the same context

If you see "discovered but failed at execution", check:
- worker capabilities
- targeting/routing config
- stale worker state after deploy/reload

## Authoring checklist for scoped bundles

- Use a stable bundle `name` in `manifest.yaml`
- Namespace all artifact IDs with that bundle name
- Keep names stable across versions where possible
- Validate with lint before deploy
- Verify with list APIs and one execution smoke test per artifact type

## Verification steps

After deploy:

1. List commands:
```bash
curl -s http://localhost:8000/api/v1/commands | jq .
```

2. List tools:
```bash
curl -s http://localhost:8000/api/v1/tools | jq .
```

3. List workflows:
```bash
curl -s http://localhost:8000/api/v1/workflows | jq .
```

4. List agents:
```bash
curl -s http://localhost:8000/api/v1/agents | jq .
```

5. Execute one namespaced artifact:
```bash
curl -X POST http://localhost:8000/api/v1/commands/my-bundle.hello/execute \
  -H "Content-Type: application/json" \
  -d '{"data": {"message": "scope smoke test"}}'
```

## Common problems

- **Artifact not listed**: wrong namespace, scope mismatch, or deploy did not load on expected workers
- **Artifact listed but not executable**: routing/capability mismatch or stale worker load state
- **Agent listed but behaves unexpectedly**: referenced tools/workflows exist but are not visible in the caller scope
- **Bundle unload leaves residue**: non-namespaced registration prevents namespace cleanup

## Related guides

- [Your First Bundle](./15a-your-first-bundle.md)
- [Building Your First Command](./15-building-your-first-command.md)
- [Workflow System](./11-workflow-system.md)
- [Worker Targeting Guide](./08a-worker-targeting-guide.md)

---

**Last Updated**: 2026-04-01
