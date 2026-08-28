# Contributing Guide

Style, tests, and workflow for people working in the Motet tree.

## External contributions

Thanks for wanting to help. We are always looking for **feedback** and for
folks who want to **pilot** Motet. Email `hello@motet.dev`.

If you want to collaborate on Motet itself — bugs, docs, design, or a future
patch — write that address too. We invite people case by case.

We are **not accepting unsolicited pull requests**. Contributor terms for the
FSL runtime are not in place yet, so we will not merge outside code until they
are.

The way to extend Motet today is a **bundle** on the Apache 2.0 SDK, not a
fork of `motet/`. See [Your First Bundle](./15a-your-first-bundle.md) and the
[SDK Reference](./38-sdk-reference.md).

The rest of this guide is for maintainers and anyone Motet has invited to work
in this repository.

## Development Workflow

### 1. Clone

```bash
git clone https://github.com/motet-ai/motet.git
cd motet
```

### 2. Create Branch

```bash
# Create feature branch
git checkout -b feature/my-feature

# Or bugfix branch
git checkout -b fix/bug-description
```

**Branch Naming**:
- `feature/` - New features
- `fix/` - Bug fixes
- `docs/` - Documentation
- `refactor/` - Code refactoring

### 3. Make Changes

Follow these guidelines:

- **Follow Code Style**: Use Black, isort, flake8, mypy
- **Write Tests**: Add tests for new features
- **Update Documentation**: Update READMEs and docstrings
- **Follow Patterns**: Use established patterns (see onboarding docs)
- **Add File Headers**: Include standardized file headers

### 4. Test Changes

```bash
# Run unit tests locally
pytest tests/unit/

# Run specific test file
pytest tests/unit/test_my_feature.py

# Run with coverage
pytest --cov=motet tests/

# Run linting
flake8 motet/
black --check motet/
isort --check motet/
mypy motet/
```

### 5. Commit Changes

Use conventional commit format:

```bash
git commit -m "feat: add new feature

- Detailed description of changes
- Reference issue numbers (#123)
- Link to relevant public documentation or issues when applicable
"
```

**Commit Types**:
- `feat:` - New feature
- `fix:` - Bug fix
- `docs:` - Documentation
- `style:` - Code style (formatting)
- `refactor:` - Code refactoring
- `test:` - Tests
- `chore:` - Maintenance

### 6. Share the change

```bash
git push origin feature/my-feature
```

Open an internal review the way the team already does. Do not treat a public
fork-and-PR as the contribution path.

## Code Style

### Formatting

```bash
# Format with Black
black motet/

# Sort imports with isort
isort motet/

# Check formatting
black --check motet/
isort --check motet/
```

### Linting

```bash
# Lint with flake8
flake8 motet/

# Type check with mypy
mypy motet/
```

### File Headers

All Python files must include standardized headers:

```python
"""
Motet - [Module Name]

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2025-11-26

Description:
    [Detailed description]

Dependencies:
    - [Key dependencies]

Usage:
    [Usage examples]

Notes:
    [Important notes]
"""
```

## Testing Requirements

### Write Tests

```python
# Unit tests for commands
def test_my_command(mock_motet):
    result = my_command.__wrapped__(data, mock_motet)
    assert result["status"] == "success"

# Integration tests run against the real stack in Docker:
#   docker-compose -f tests/docker-compose.test.yml run --rm test-runner
@pytest.mark.integration
def test_distributed_execution():
    from motet_sdk.testing import MockMotetContext
    result = my_command.__wrapped__(data=MyData(...), motet=MockMotetContext())
    assert result["result"] == "success"
```

### Test Coverage

- **New Features**: Must have tests
- **Bug Fixes**: Must have regression tests
- **Coverage**: Maintain or improve coverage
- **All Tests Pass**: Ensure all tests pass before sharing a change

## Documentation Requirements

### Update READMEs

Before committing, check and update READMEs:

1. **Check Parent README**: Read README in parent directory
2. **Update if Needed**: Update to reflect code changes
3. **Verify Examples**: Ensure examples work with current code
4. **Update Architecture**: Update architecture references if needed

### Add Docstrings

```python
def my_function(param: str) -> Dict[str, Any]:
    """
    Function description.
    
    Args:
        param: Parameter description
    
    Returns:
        Return value description
    
    Raises:
        ValueError: When parameter is invalid
    """
    pass
```

### Update Documentation

- Update relevant documentation sections
- Add examples for new features
- Update architecture diagrams if needed
- Link to relevant docs when applicable

## Review checklist

Use this when a change is invited (maintainers, or a patch Motet asked for).
Unsolicited external pull requests are not accepted.

### Before sharing a change

1. ✅ All tests pass
2. ✅ Code formatted (Black, isort)
3. ✅ Linting passes (flake8, mypy)
4. ✅ Documentation updated
5. ✅ File headers added/updated (`Last Modified` only — no `Created:`)
6. ✅ READMEs checked and updated

### Change notes

1. **Clear Description**: Explain what and why
2. **Testing Notes**: Explain how to test
3. **Breaking Changes**: Note any breaking changes

### Review Checklist

Reviewers check:
- ✅ Code follows style guidelines
- ✅ Tests are comprehensive
- ✅ Documentation is updated
- ✅ No breaking changes (or documented)
- ✅ Follows established patterns
- ✅ Security considerations addressed

## Architecture

See the [Architecture Guide](./31-architecture-guide.md) for a product-level map of commands, agents, models, tools, and workflows.

## Best Practices

### 1. Follow Established Patterns

```python
# ✅ CORRECT: Use decorator pattern
@motet.command()
def my_command(data: MyData, motet: MotetContext):
    pass

# ❌ WRONG: Don't create new patterns without documenting the decision
```

### 2. Write Comprehensive Tests

```python
# ✅ CORRECT: Test success and failure cases
def test_success(mock_motet):
    pass

def test_error_handling(mock_motet):
    pass
```

### 3. Update Documentation

Update the README in the directory you changed, and check the parent one too —
a new command usually belongs in both.

### 4. Use Type Hints

```python
# ✅ CORRECT: Use type hints
def my_function(param: str) -> Dict[str, Any]:
    pass
```

## Getting Help

- **Email**: `hello@motet.dev` — feedback, pilots, or to collaborate (invite-only)
- **Documentation**: Review relevant sections
- **Code Examples**: Check `motet-sdk/examples/bundles/`

## Next Steps

- **[Project Structure](./33-project-structure.md)** - Understand codebase
- **[Resources & Links](./34-resources-links.md)** - Additional resources
- **[Architecture Guide](./31-architecture-guide.md)** — the product-level map

## Navigation

- **[← Back to Documentation Home](./00-landing-page.md)** - Main documentation hub

---

**Last Updated**: 2026-08-27
