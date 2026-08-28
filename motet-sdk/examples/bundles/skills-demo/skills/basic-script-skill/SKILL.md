---
name: basic-script-skill
description: >
  Motet demo skill that runs a bundled Python script via a skill runner
  and returns a JSON envelope. Use to verify skill deployment and script execution.
---

# Basic Script Skill

Use this skill to verify that bundled skill files are deployed and that scripts
inside the skill directory can run in the worker environment.

## How To Use

Prefer the registered runner tool `skills-demo.basic-script-skill.echo` with a
`text` argument. Or run the script directly:

```bash
python scripts/echo_payload.py --help
python scripts/echo_payload.py --text "bundle-skill-smoke"
```

## Expected Behavior

- Script prints a single JSON object to stdout.
- JSON includes `ok: true`, `message` with your input text, and `source`
  identifying this skill (`skills-demo.basic-script-skill`).
