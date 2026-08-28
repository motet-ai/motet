# Test warnings – why they appear and how to reduce them

## Why there are so many warnings

1. **Pytest config not loaded from repo root**  
   Pytest is usually run as `pytest tests/` from the project root. Pytest only reads config from the **rootdir** (the project root), not from `tests/`. Your pytest config lives in `tests/pytest.ini`, so when you run from root that file is **not** loaded. Effects:
   - **Unknown pytest.mark (health, monitoring, alerting)** – those markers are defined in `tests/pytest.ini`, which isn’t used from root, so pytest doesn’t know them.
   - **No warning filters** – the `filterwarnings` in `tests/pytest.ini` are never applied when running from root.

2. **Wrong section in `tests/pytest.ini`**  
   In a `pytest.ini` file the section must be `[pytest]`. Using `[tool:pytest]` (for `setup.cfg`/pyproject) means the file is not recognized as valid pytest.ini, so even when pytest looks in `tests/`, the options may not apply.

3. **Deprecations and third‑party warnings**  
   Even with a loaded config, several warnings come from your code or dependencies:
   - **FastAPI `on_event`** – `motet/interfaces/http.py` uses `@app.on_event("startup")` / `("shutdown")`, which FastAPI has deprecated in favour of lifespan context managers.
   - **Redis `close()`** – `orchestrator.py` uses `await redis_client.close()`; the client recommends `aclose()`.
   - **Torch / transformers / Pydantic** – warnings from installed libraries (e.g. pynvml, `clean_up_tokenization_spaces`, `validation_alias`).

4. **Async fixture usage**  
   An async fixture (`test_orchestrator`) is used by an async test; with `asyncio_mode = auto` and strict mode, pytest-asyncio warns about this pattern.

## What was changed to reduce warnings

- **Root `pytest.ini`** added at project root with `[pytest]`, so runs from root load it. It includes:
  - The same **markers** as in `tests/pytest.ini` (so `health`, `monitoring`, `alerting` are known).
  - **filterwarnings** so that:
    - DeprecationWarning / PendingDeprecationWarning are ignored.
    - Unknown pytest mark warnings are ignored.
    - Optional: you can add more filters for third‑party warnings (see below).

- **`tests/pytest.ini`** section corrected from `[tool:pytest]` to `[pytest]` so that config is valid when pytest is run from `tests/`.

Running `pytest tests/` from the repo root should now apply the root config and cut most of the marker and deprecation noise. Remaining warnings are from FastAPI/Redis/third‑party code and can be silenced with extra `filterwarnings` or fixed in code (e.g. migrating to lifespan and `aclose()`).

## Optional: filter more third‑party warnings

In the root `pytest.ini` you can add to `filterwarnings`:

```ini
filterwarnings =
    ignore::DeprecationWarning
    ignore::PendingDeprecationWarning
    ignore:.*Unknown pytest.mark.*:pytest.PytestUnknownMarkWarning
    ignore:.*on_event is deprecated.*:DeprecationWarning
    ignore:.*Use aclose.*:DeprecationWarning
    ignore::FutureWarning
```

That will further reduce FastAPI/Redis and other `FutureWarning`/deprecation output.
