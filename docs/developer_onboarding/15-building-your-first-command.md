# Building Your First Command

A command is the unit of work in Motet. This tutorial builds one end to end — a text analyser that counts words, guesses at sentiment, and remembers what it saw — and deploys it so workers can run it.

## Where your command should live

Custom commands belong in a **bundle**: a directory you deploy to a running Motet, which loads and registers its contents without a rebuild. That is the path this tutorial takes.

The alternative is adding a command to `motet/core/commands/builtin/` when you are changing the framework itself. That path is not open to external pull requests — see [Contributing](./32-contributing-guide.md). If you are building on Motet, stay on the bundle path. Maintainers adding a built-in have one extra registration step — see [Adding a built-in](#adding-a-built-in) at the end.

## Scaffold a bundle

```bash
motet-cli bundle init text-tools
cd text-tools
```

You get `manifest.yaml` alongside `commands/`, `tools/`, `agents/`, `workflows/`, and `config/` directories. Only `manifest.yaml` and `commands/` matter here. The `name` in `manifest.yaml` is the namespace for everything the bundle contains, so a `text_analysis` command in a bundle named `text-tools` is addressed as `text-tools.text_analysis`.

[Your First Bundle](./15a-your-first-bundle.md) covers the layout in full. Come back here once the directory exists.

## Describe the input

Every command declares what it accepts as a Pydantic model. The runtime validates incoming payloads against it before your function is called, so by the time you have a `data` object, its fields are the types you asked for.

Create `commands/text_analysis.py`:

```python
from __future__ import annotations

from typing import Any, Dict

from pydantic import Field

from motet_sdk import BaseCommandData, MotetContext, motet

class TextAnalysisData(BaseCommandData):
    """Input for the text analysis command."""

    text: str = Field(..., description="Text to analyze")
    store_in_memory: bool = Field(default=True, description="Store results in memory")
    tags: list[str] = Field(
        default_factory=lambda: ["analysis"],
        description="Tags applied to the stored memory",
    )
```

Write the `description` on each field as though a model will read it, because one will. Field descriptions are indexed for discovery, as is the first line of your command function's docstring — that is how an agent searching the catalog decides your command is the one it wants. [Descriptions are indexed for discovery](./21-tool-ecosystem.md#descriptions-are-indexed-for-discovery) explains what that indexing does.

## Write the function

The decorator makes this a distributed command. The first parameter is bound to your data model and the second receives the context; both need type hints, since that is how the decorator knows which is which.

```python
@motet.command(timeout_seconds=60)
def text_analysis(data: TextAnalysisData, motet: MotetContext) -> Dict[str, Any]:
    """Analyze text for length, word frequency, and rough sentiment."""
    import re
    from collections import Counter

    words = re.findall(r"\b\w+\b", data.text.lower())

    positive = {"good", "great", "excellent", "wonderful", "amazing"}
    negative = {"bad", "terrible", "awful", "horrible", "worst"}
    hits_positive = sum(1 for w in words if w in positive)
    hits_negative = sum(1 for w in words if w in negative)

    if hits_positive > hits_negative:
        sentiment = "positive"
    elif hits_negative > hits_positive:
        sentiment = "negative"
    else:
        sentiment = "neutral"

    return {
        "word_count": len(data.text.split()),
        "char_count": len(data.text),
        "sentence_count": len(re.split(r"[.!?]+", data.text)),
        "most_common_words": [
            {"word": w, "count": c} for w, c in Counter(words).most_common(5)
        ],
        "sentiment": sentiment,
        "sentiment_scores": {"positive": hits_positive, "negative": hits_negative},
    }
```

Return plain data. You do not wrap it in a status envelope or a result object — the decorator does that, so every command in the system answers in the same shape. Raise on failure and the decorator records the error the same way.

## Reach the rest of Motet

The `motet` parameter is how a command gets at everything else: memory, tools, the vault, models, and other commands. Nothing is imported and nothing is constructed, which is what lets the same function run in a worker, in a test, and under a different tenant without changing.

Storing the analysis and looking for earlier ones takes two calls:

```python
@motet.command(timeout_seconds=60)
def text_analysis(data: TextAnalysisData, motet: MotetContext) -> Dict[str, Any]:
    """Analyze text for length, word frequency, and rough sentiment."""
    results = analyse(data.text)  # the body from the previous step

    if data.store_in_memory:
        motet.memory.store(
            content=f"Text analysis: {data.text[:100]}",
            tags=data.tags + ["text_analysis"],
            metadata={
                "word_count": results["word_count"],
                "sentiment": results["sentiment"],
            },
        )

    previous = motet.memory.recall(tags=data.tags, limit=3)
    if previous:
        results["similar_analyses"] = len(previous)

    return results
```

Note what is absent: no tenant, no principal, no conversation id. The context carries the caller's identity, and memory is namespaced by it automatically. Passing those yourself is not how isolation works here — [Memory Management](./20-memory-management.md) covers the scoping rules.

For calling other commands from inside this one, see [Command Composition Patterns](./16-command-composition-patterns.md).

## Handle failure

Validate what Pydantic cannot express, log with enough structure to find the failure later, and let the exception travel:

```python
import structlog

logger = structlog.get_logger(__name__)

@motet.command(timeout_seconds=60)
def text_analysis(data: TextAnalysisData, motet: MotetContext) -> Dict[str, Any]:
    """Analyze text for length, word frequency, and rough sentiment."""
    if not data.text.strip():
        raise ValueError("Text cannot be empty")
    if len(data.text) > 100_000:
        raise ValueError("Text too long (max 100KB)")

    logger.info("text_analysis_started", text_length=len(data.text))
    return analyse(data.text)
```

Do not catch an exception only to log it and re-raise unchanged; the decorator already reports failures with the command id and worker attached. Catch when you can do something about it — a fallback, a retry, a partial result — and otherwise let it go.

## Test it

Call `__wrapped__` to reach the undecorated function, and hand it `MockMotetContext` from the SDK. No worker, no Redis, no running stack.

Create `tests/test_text_analysis.py` in your bundle:

```python
import pytest
from motet_sdk.testing import MockMotetContext

from commands.text_analysis import TextAnalysisData, text_analysis

def test_counts_words():
    result = text_analysis.__wrapped__(
        data=TextAnalysisData(text="This is a test sentence."),
        motet=MockMotetContext(),
    )
    assert result["word_count"] == 5
    assert result["sentiment"] == "neutral"

def test_rejects_empty_text():
    with pytest.raises(ValueError, match="Text cannot be empty"):
        text_analysis.__wrapped__(
            data=TextAnalysisData(text="   "),
            motet=MockMotetContext(),
        )
```

```bash
pytest tests/test_text_analysis.py -v
```

A passing unit test tells you the logic is right. It does not tell you the command is reachable — that takes a deploy, which is next. [Testing Strategies](./18-testing-strategies.md) covers the rest.

## Deploy and run it

Lint first, since the deploy pipeline runs the same checks and will reject the bundle otherwise:

```bash
motet-cli bundle lint .
motet-cli bundle hot-deploy .
```

`hot-deploy` is the fast local loop. Use `motet-cli deploy dir-deploy .` for a full deploy, and see [Your First Bundle](./15a-your-first-bundle.md) for git-based deploys, targeting, and rollback.

Once deployed, the command answers on its namespaced type:

```bash
curl -X POST http://localhost:8000/api/v1/commands/text-tools.text_analysis/execute \
  -H "Content-Type: application/json" \
  -d '{"data": {"text": "This is a wonderful day! The weather is great.", "tags": ["test"]}}'
```

Confirm it registered with `GET /api/v1/commands`, which lists `text-tools.text_analysis` among the available types.

| Method | Path | Purpose |
|--------|------|---------|
| GET | /api/v1/commands | List registered command types |
| GET | /api/v1/commands/{command_type} | Describe one command and its schema |
| POST | /api/v1/commands/{command_type}/execute | Run a command |

## Watch it run

```bash
motet-cli local logs
```

The management UI at `http://localhost:8000/manage` shows the command executing, which worker took it, and how long it ran. Setting `MOTET_DEBUG_MODE=true` adds detail to the trace.

## The complete command

```python
"""
Text Analysis Command

Analyzes text and optionally stores the result in memory.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Dict

import structlog
from pydantic import Field

from motet_sdk import BaseCommandData, MotetContext, motet

logger = structlog.get_logger(__name__)

class TextAnalysisData(BaseCommandData):
    """Input for the text analysis command."""

    text: str = Field(..., description="Text to analyze")
    store_in_memory: bool = Field(default=True, description="Store results in memory")
    tags: list[str] = Field(
        default_factory=lambda: ["analysis"],
        description="Tags applied to the stored memory",
    )

@motet.command(timeout_seconds=60)
def text_analysis(data: TextAnalysisData, motet: MotetContext) -> Dict[str, Any]:
    """Analyze text for length, word frequency, and rough sentiment."""
    if not data.text.strip():
        raise ValueError("Text cannot be empty")
    if len(data.text) > 100_000:
        raise ValueError("Text too long (max 100KB)")

    logger.info("text_analysis_started", text_length=len(data.text))

    words = re.findall(r"\b\w+\b", data.text.lower())

    positive = {"good", "great", "excellent", "wonderful", "amazing"}
    negative = {"bad", "terrible", "awful", "horrible", "worst"}
    hits_positive = sum(1 for w in words if w in positive)
    hits_negative = sum(1 for w in words if w in negative)

    if hits_positive > hits_negative:
        sentiment = "positive"
    elif hits_negative > hits_positive:
        sentiment = "negative"
    else:
        sentiment = "neutral"

    word_count = len(data.text.split())
    results: Dict[str, Any] = {
        "word_count": word_count,
        "char_count": len(data.text),
        "sentence_count": len(re.split(r"[.!?]+", data.text)),
        "most_common_words": [
            {"word": w, "count": c} for w, c in Counter(words).most_common(5)
        ],
        "sentiment": sentiment,
        "sentiment_scores": {"positive": hits_positive, "negative": hits_negative},
    }

    if data.store_in_memory:
        motet.memory.store(
            content=f"Text analysis: {data.text[:100]}",
            tags=data.tags + ["text_analysis"],
            metadata={"word_count": word_count, "sentiment": sentiment},
        )

    previous = motet.memory.recall(tags=data.tags, limit=3)
    if previous:
        results["similar_analyses"] = len(previous)

    logger.info("text_analysis_completed", word_count=word_count, sentiment=sentiment)
    return results
```

## Adding a built-in

If you already work in this tree and are adding a command under `motet/core/commands/builtin/`, there is one more step, and skipping it fails in a way no test will catch.

Import lists in `DistributedCommand._ensure_commands_registered()` are the only thing that registers built-in command types. A module sitting in that directory but missing from that list is not registered: workers reject it at runtime with "Unknown command type", while your unit tests keep passing, because they import the function directly and never go near a worker.

Add the import, restart the stack, and the command is available. Bundles have no equivalent step — the loader registers what it finds.

## Next steps

- **[Your First Bundle](./15a-your-first-bundle.md)** — the full bundle workflow, including tools, agents, and workflows
- **[Bundle Scoping and Visibility](./15b-bundle-scoping-and-visibility.md)** — who can see and run what you deployed
- **[Command Composition Patterns](./16-command-composition-patterns.md)** — calling commands from commands, in sequence and in parallel
- **[Testing Strategies](./18-testing-strategies.md)** — testing beyond the unit level

## Navigation

- **[← Back to Documentation Home](./00-landing-page.md)** - Main documentation hub

---

**Ready for more?** Continue to [Your First Bundle](./15a-your-first-bundle.md).
