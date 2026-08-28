# Motet - Agent Instructions (evaluation snapshot)

This repository is a Motet evaluation snapshot. See `PUBLIC_RELEASE.md` for the
release version and `EVALUATION_TERMS.md` for access terms.

## Architecture (start here)

For architecture questions, read `docs/architecture/current/README.md` first, then
**only the one chapter you need**. That tree is the runtime contract: topology,
names, paths, and invariants. Do not glob the folder.

Developer onboarding (`docs/developer_onboarding/`) is the product surface: how to
build bundles, call APIs, and operate the stack.

## Tests

- Unit tests run locally: `pytest tests/unit/`
- Integration, API, and E2E tests require the Docker stack:
  `docker-compose -f tests/docker-compose.test.yml run --rm test-runner`

## Boundaries

- Evaluation use only; see `EVALUATION_TERMS.md` (including the AI/ML training and
  retention restrictions before submitting source to third-party services).
- Unsolicited pull requests are not accepted; extend Motet with a bundle instead
  (`docs/developer_onboarding/15a-your-first-bundle.md`).
