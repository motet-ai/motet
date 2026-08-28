# Skills Demo (example bundle)

SDK example for Motet **Agent Skills**: progressive disclosure via a compact
skill catalog, `core.activate_skill`, and a Motet runner tool that executes a
bundled script.

Anthropic’s Apache-2.0 reference skills are **committed** here. Proprietary
document skills (`pdf` / `docx` / `pptx` / `xlsx`) are **not** redistributed by
Motet — fetch them locally from [anthropics/skills](https://github.com/anthropics/skills)
with `./scripts/fetch-skills.sh` (gitignored). See [`THIRD_PARTY_SKILLS.md`](./THIRD_PARTY_SKILLS.md).

## What you get

| Piece | Purpose |
|-------|---------|
| Agent `skills-demo.skills` | Chat agent with allowlisted skills + discovery tools |
| Skill `basic-script-skill` | Motet-authored demo; runner `skills-demo.basic-script-skill.echo` |
| Skills `brand-guidelines`, `frontend-design`, `theme-factory`, `algorithmic-art` | Apache Anthropic reference skills (`core.activate_skill`) |
| `scripts/fetch-skills.sh` | Optional local clone of document skills (not committed) |

## Layout

```text
skills-demo/
├── manifest.yaml
├── config/exec.yaml          # base_image_stack: python-minimal
├── agents/agents.yaml
├── THIRD_PARTY_SKILLS.md
├── scripts/fetch-skills.sh
└── skills/
    ├── basic-script-skill/   # Motet runner (echo)
    ├── algorithmic-art/
    ├── brand-guidelines/
    ├── frontend-design/
    └── theme-factory/
```

## Prerequisites

- Local Motet stack (`motet-cli local up` or equivalent)
- A configured model/provider for agent chat

No vault secrets required for the committed example.

## Deploy

```bash
motet-cli bundle lint motet-sdk/examples/bundles/skills-demo
motet-cli deploy dir-deploy motet-sdk/examples/bundles/skills-demo
```

After reload you should see:

- Agent: `skills-demo.skills` (alias `skills-demo`)
- Skills: `skills-demo.basic-script-skill`, `.brand-guidelines`, `.frontend-design`,
  `.theme-factory`, `.algorithmic-art`
- Tool: `skills-demo.basic-script-skill.echo`

## Try (Chat Explorer)

1. Open Chat Explorer and select **Skills Demo** (`skills-demo.skills`).
2. Smoke-test the runner:

   > Call the skills-demo.basic-script-skill.echo runner with text "sticky".

   Expect a JSON envelope with `ok: true`, `message: "sticky"`, and
   `source: "skills-demo.basic-script-skill"`.

3. Activate an instruction skill:

   > Activate the brand-guidelines skill and summarize how Motet (as a product)
   > should present primary vs secondary colors using that skill’s guidance.

   The agent should call `core.activate_skill` for `skills-demo.brand-guidelines`
   before answering from the skill body.

## Optional: fetch document skills (local only)

```bash
./motet-sdk/examples/bundles/skills-demo/scripts/fetch-skills.sh
# or a subset:
./motet-sdk/examples/bundles/skills-demo/scripts/fetch-skills.sh pdf
```

Then redeploy the same path. `agents/agents.yaml` `skill_ids` are synced to every
`skills/*/SKILL.md` present. Document skill folders are **gitignored** — do not
commit them.

**License:** those skills are Anthropic source-available / proprietary. Motet is
not redistributing them; you are cloning from Anthropic’s public repo for local
use. Read each skill’s `LICENSE.txt`.

**Runtime note:** catalog + `core.activate_skill` works on `python-minimal`. Full
LibreOffice / poppler / office script pipelines need a pinned `python-office`
image stack and workspace containers (below).

## Optional: build and pin `python-office`

Document skill **scripts** (soffice, pdftoppm, pandoc, npm `docx`, …) need the
platform `python-office` stack. Motet ships the stack **name** but not a default
OCI image — build one locally:

```bash
./scripts/build-python-office-stack.sh
# → tags motet/python-office:dev and prints pin instructions
```

1. Add to `.env` (must reach **motet-api** and **workers**), then recreate those
   services:

   ```bash
   MOTET_IMAGE_STACK_PYTHON_OFFICE=motet/python-office:dev
   ```

2. After `fetch-skills.sh`, set in this bundle’s `config/exec.yaml`:

   ```yaml
   base_image_stack: python-office
   ```

   (Committed default stays `python-minimal` so the example deploys without the
   office image.)

3. Redeploy:

   ```bash
   motet-cli deploy dir-deploy motet-sdk/examples/bundles/skills-demo
   ```

Details: [`docker/images/python-office/README.md`](../../../../docker/images/python-office/README.md).

## Troubleshooting

| Symptom | Likely cause |
|---------|----------------|
| Lint / deploy fails on missing script | Runner `script:` paths are relative to the skill directory |
| Echo tool missing after deploy | Redeploy; confirm bundle id `skills-demo` in `motet-cli deploy list` |
| Document skills missing | Run `scripts/fetch-skills.sh`, then `dir-deploy` again |
| Alias collision on reload (`main` already points to …) | This bundle uses agent id `skills` (`skills-demo.skills`), not `main` |
| Office scripts fail at runtime | Build/pin `python-office` (above); set `base_image_stack: python-office`; ensure workspace containers can pull/use the local tag |

## Non-goals

- Not a substitute for the internal fixture
  [`tests/bundles/skills-vendor-demo`](../../../../tests/bundles/skills-vendor-demo)
  (full upstream tree for Motet CI / skills loading tests).
- Does not ship proprietary document skill trees in Motet’s repository or SDK
  examples.
- Does not ship a prebuilt `python-office` / `python-browser` OCI image in the
  default compose stack (operators build or pin their own).
