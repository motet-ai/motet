## Package: skills

**Bundle-resident and filesystem SKILL.md lifecycle** — parse, register, assemble catalog text for the model, and activate explicit skills per turn. Canonical **`SkillRef`** rows ride **`LLMRequest`** for tracing; adapters do not reinterpret them.

### Purpose

- **Discovery**: Locate `SKILL.md` docs for loaded bundles (and filesystem refresh where configured).
- **Registration**: Maintain an in-process **`SkillRegistry`** keyed by **`{bundle_id}.{name}`** (`name` is the SKILL frontmatter slug).
- **Activation**: Respect **`AgentConfig.skill_mode`** plus explicit **`skill_ids`** when building the injected skill context for a turn.
- **Runners**: Optional YAML-declared **`RunnerSpec`** lists so skills can advertise argv-style tooling without embedding Python in SKILL bodies.

### Core components

#### Parsing (`parser.py`)

Frontmatter plus markdown body parsing into **`ParsedSkillDoc`** for downstream assembly and hashing.

#### Filesystem (`filesystem.py`)

Refresh and enumerate filesystem-backed skills where the deployment enables that path.

#### Registry (`registry.py`)

**`RegisteredSkill`**, **`get_skill_registry`**, and mutations used when bundles load/unload.

#### Assembly (`assembly.py`)

**`assemble_skills_for_turn`**, **`build_skill_catalog_for_turn`**, explicit activation helpers (**`activate_explicit_skills_for_turn`**, **`activate_skill_record`**), and **`find_skill_by_name_or_id`** for lookups.

#### Runners (`runners.py`, `runtime.py`)

Parse **`RunnersDoc`** / YAML runner lists and register runnable hooks (**`register_runners_for_skill`**) consumed when a skill invokes configured tooling.

### Notes

- Skill identifiers are stable **`{bundle_id}.{name}`** strings; align HTTP/API payloads and tests with that form.
- For HTTP surface (list/import/activation), see **`motet/interfaces/api/v1/skills.py`**.

### Related

- SDK and bundle authoring: **`motet-sdk/`** and **`docs/developer_onboarding/`**.
