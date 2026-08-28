# content-review

Multi-perspective content review pipeline demonstrating `motet.join()` with different commands, `motet.maybe()` for optional steps, and sequential `motet.do()` chains.

## What it showcases

| Capability | Where demonstrated |
|---|---|
| **`motet.join()` with different commands** | `coordinate_reviews` — runs grammar, tone, and accuracy reviews in parallel |
| **`motet.maybe()` for optional steps** | `coordinate_reviews` — SEO review gracefully skipped on failure |
| **Sequential `motet.do()` chains** | `coordinate_reviews` — synthesis then revision in sequence |
| **Multiple LLM perspectives** | Four review commands with distinct prompts on the same content |
| **Declarative vs programmatic** | `content_review.yaml` workflow achieves the same pipeline as `coordinate_reviews` |
| **LLM inference** (`motet.models.infer`) | All review, synthesis, and revision commands |

## Pipeline

```
          ┌─ review_grammar ──────┐
          ├─ review_tone ─────────┤
content ──┤                       ├──▶ synthesize_feedback ──▶ revise_content
          ├─ review_accuracy ─────┤
          └─ review_seo (optional)┘
```

## Commands

### review_grammar
Checks grammar, spelling, punctuation, and sentence structure. Returns issues with severity levels (error/warning/suggestion) and a score.

### review_tone
Evaluates voice, audience appropriateness, readability, and engagement. Takes an `audience` parameter that shapes evaluation criteria.

### review_accuracy
Fact-checks claims and flags unsupported or questionable assertions. Returns claims with confidence levels (verified/plausible/questionable/unsupported).

### review_seo
Analyzes keyword usage, heading structure, and search-engine readability. Intentionally used as the *optional* step in `coordinate_reviews` to demonstrate `motet.maybe()`.

### synthesize_feedback
Combines all review results into a single prioritized feedback report using LLM inference. Handles missing SEO data gracefully when it was skipped.

### revise_content
Rewrites the original content incorporating the synthesized feedback. Preserves the author's voice and intent while fixing identified issues.

### coordinate_reviews
**The showcase command.** Orchestrates the full pipeline programmatically:

```python
# Step 1: Three reviews in parallel (motet.join with DIFFERENT commands)
grammar, tone, accuracy = motet.join([
    (review_grammar, ReviewGrammarData(content=data.content)),
    (review_tone, ReviewToneData(content=data.content, audience=data.audience)),
    (review_accuracy, ReviewAccuracyData(content=data.content)),
])

# Step 2: Optional SEO review (motet.maybe — pipeline continues on failure)
seo, seo_error = motet.maybe(review_seo, data=ReviewSeoData(...))

# Step 3: Synthesize feedback (sequential motet.do)
feedback = motet.do(synthesize_feedback, data=SynthesizeFeedbackData(...))

# Step 4: Revise content (sequential motet.do)
revised = motet.do(revise_content, data=ReviseContentData(...))
```

## Workflow

The `content_review` workflow defines the same pipeline declaratively:

```yaml
review_grammar ──┐
review_tone ─────┤
                 ├──▶ synthesize_feedback ──▶ revise_content
review_accuracy ─┤
review_seo ──────┘
```

Steps with no dependencies between them run in parallel automatically.

Invoke via the agent:

> "Review my blog post and suggest improvements"

Or directly:

```
workflow_content-review__content_review(content="Your draft here...", audience="developers")
```

## Configuration

| Parameter | Default | Description |
|---|---|---|
| `content` | *(required)* | Content to review and revise |
| `audience` | general | Target audience |
| `content_type` | article | Content type (article, blog_post, etc.) |
| `provider` | openai | LLM provider |
| `model_name` | gpt-4o-mini | LLM model |
