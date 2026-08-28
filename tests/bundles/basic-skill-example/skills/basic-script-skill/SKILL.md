---
name: basic-script-skill
description: Basic test skill that runs a bundled Python script and returns JSON output.
---

# Basic Script Skill

Use this skill to verify that bundled skill files are deployed and that scripts inside
the skill directory can run in the worker environment.

## How To Use

1. Run the script help first:

```bash
python scripts/echo_payload.py --help
```

2. Run the script with test text:

```bash
python scripts/echo_payload.py --text "bundle-skill-smoke"
```

## Expected Behavior

- Script prints a single JSON object to stdout.
- JSON includes:
  - `ok: true`
  - `message` with your input text
  - `source` identifying this skill bundle

If this works, skill deployment and script execution are both wired correctly.
