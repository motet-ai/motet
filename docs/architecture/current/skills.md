# Skills

A skill is a folder with a `SKILL.md` (public Agent Skills format) and, optionally, scripts. The model sees a compact catalog of names and descriptions until it calls `core.activate_skill`, which loads the full body. Dozens of skills cost a couple of lines each until one is relevant.

Skill ids are `{bundle_id}.{name}`, where `name` is the `SKILL.md` frontmatter slug.

## Where they load from

- Bundles: `skills/<name>/SKILL.md`
- Disk: `.agents/skills` (on by default)

`motet-cli skills list` shows what is loaded. `motet-cli bundle lint` checks a `SKILL.md` against the public constraints before deploy.

Vendor skill folders (`pdf`, `xlsx`, `pptx`, `docx`, `mcp-builder`, …) are used unmodified as a test fixture. Compatibility with the public format is intentional.

## Activation and scripts

`core.activate_skill` is the only activation path. Provider-native skill hosting is not wired — a skill does not get handed to a vendor that hosts skills itself.

Script-backed skills, after activation, force-include `core.workspace_shell_exec`. That tool:

- Materializes the skill files into a workspace container under `/scratch/skills/<skill>/`
- Installs Python requirements the bundle declares
- Bridges artifacts in and out
- Returns exit status plus files the script produced

Script-backed skills need Docker. Text-only skills run on a bare worker.

Optional `scripts.yaml` / `usage.yaml` beside `SKILL.md` is guidance for the model. `SKILL.md` remains the canonical document.

## Runners

A skill may also declare **runners** in `runners.yaml` beside `SKILL.md`: named entrypoints that register as first-class tools when the skill loads, named `{bundle_id}.{skill_name}.{runner_name}`. Each runner points at a script exposing `handle(params)` and declares a `lifetime`:

- `ephemeral` — fresh execution per call
- `workspace` — persistent `/scratch` shared across the conversation
- `stateful` — warm supervisor keeps module-level state alive between calls

Registration is idempotent. Runner execution rides on workspace containers — see [execution-workspaces.md](./execution-workspaces.md).

## Paths

- Package: `motet/core/skills/` (`parser.py`, `registry.py`, `assembly.py`, `filesystem.py`)
- Tool: `core.activate_skill`
- Workspace exec: `core.workspace_shell_exec`
- Onboarding inventory: `docs/developer_onboarding/03-what-motet-can-do.md` (Skills)
