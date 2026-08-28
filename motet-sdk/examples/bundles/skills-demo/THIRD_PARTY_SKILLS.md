# Third-party Agent Skills

## Committed (Apache-2.0)

These skill packages under `skills/` are copied from Anthropic's public reference
repository and are licensed under Apache License 2.0 (see each folder's
`LICENSE.txt`):

- **Repository:** https://github.com/anthropics/skills.git
- **Git ref:** `main`
- **Commit:** `98669c11ca63e9c81c11501e1437e5c47b556621`

**Committed skill packages:**

- `skills/algorithmic-art/`
- `skills/brand-guidelines/`
- `skills/frontend-design/`
- `skills/theme-factory/`

See the upstream [README](https://github.com/anthropics/skills/blob/main/README.md).

## Motet-authored

- `skills/basic-script-skill/` — Motet first-party demo (runner + script); not from Anthropic.

## Optional fetch (not redistributed by Motet)

Document skills (`pdf`, `docx`, `pptx`, `xlsx`) are **source-available / proprietary**
under Anthropic's per-skill `LICENSE.txt`. Motet does **not** commit them into this
example. Fetch them locally with:

```bash
./scripts/fetch-skills.sh
```

Those folders are gitignored. Do not commit them into Motet or any Motet public
export. Not legal advice — read each skill's `LICENSE.txt` before use.
