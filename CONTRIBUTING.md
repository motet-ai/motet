# Contributing to Motet

## External contributions

Thanks for wanting to help. We are always looking for **feedback** and for
folks who want to **pilot** Motet. Email `hello@motet.dev` — same address as
[commercial licensing](COMMERCIAL_LICENSE.md) and
[evaluation terms](EVALUATION_TERMS.md).

If you want to collaborate on Motet itself — bugs, docs, design, or a future
patch — write that address too. We invite people case by case.

We are **not accepting unsolicited pull requests**. Contributor terms for the
FSL runtime are not in place yet, so we will not merge outside code until they
are.

The way to extend Motet today is a **bundle** on the Apache 2.0 SDK, not a
fork of `motet/`. Start with [Your first bundle](docs/developer_onboarding/15a-your-first-bundle.md)
and the [SDK reference](docs/developer_onboarding/38-sdk-reference.md).

## Licensing

Motet uses a split license model:

- **Runtime (`motet/`)**: [Functional Source License, Version 1.1 (FSL-1.1-ALv2)](LICENSE-FSL),
  or a commercial license. Each published runtime version converts to Apache 2.0
  two years after that version is made available.
- **SDK (`motet-sdk/`)**: [Apache License, Version 2.0](motet-sdk/LICENSE).

Working in this tree does not start an FSL conversion clock. Publication of a
version does. File-header dates are not license dates.

## Working in this tree

The rest of this file is for people who already have write access (maintainers
and anyone we have invited). Style, tests, and setup still apply.

### Prerequisites

- Python 3.11+
- Docker and Docker Compose (for integration tests)
- Redis (for distributed features)
- PostgreSQL with pgvector (for vector memory)

### Development setup

```bash
# Clone the repository
git clone https://github.com/motet-ai/motet.git
cd motet

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate

# Install in development mode (runtime + SDK)
pip install -e . -e motet-sdk

# Copy environment template
cp .env.example .env  # Edit with your settings
```

### Running tests

```bash
# Unit tests (can run locally)
pytest tests/unit/ -v

# Integration tests (MUST use Docker)
docker-compose -f tests/docker-compose.test.yml run --rm test-runner

# Rebuild test runner (only after dependency changes)
docker-compose -f tests/docker-compose.test.yml build test-runner
```

### Code style

- Follow PEP 8. Use type hints, Pydantic models, `structlog`, and f-strings.
- Use enum names instead of magic numbers.
- Format with Black and isort; lint with flake8; type-check with mypy.

```bash
black motet/
isort motet/
flake8 motet/
mypy motet/
```

Python files use the standardized header in
[the contributing guide](docs/developer_onboarding/32-contributing-guide.md#file-headers).
Do not add a `Created:` date.

### Commit messages

Use conventional commits: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`,
`perf`, `ci`.

### Error handling

- Never use bare `except:` or `except Exception: pass`.
- Log errors with full context before re-raising.
- Fail fast and loud.

More detail: [Contributing guide](docs/developer_onboarding/32-contributing-guide.md).
