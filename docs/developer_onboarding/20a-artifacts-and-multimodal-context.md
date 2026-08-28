# Artifacts and Multimodal Context

Artifacts are **large, raw, or binary payloads** stored outside the hot conversational memory path. They support **user file uploads** (PDF, images, etc.) and **tool outputs** that are referenced by ID in conversation and rendered into model context when needed. This section covers the artifact model, the Artifacts API, and how multimodal context (images, extracted text) is injected into LLM turns.

## Why Artifacts?

- **Token and safety**: Avoid inlining raw bytes or base64 into prompts; reference by ID and fetch only when building context.
- **Schema correctness**: Tool results and uploads are stored in a provider-neutral way; provider adapters render them into the correct message format (e.g. OpenAI image parts, Claude blocks) at the boundary.
- **Replay and audit**: Stable `artifact_id`s and metadata allow deterministic reconstruction of context and conversation.

## Two-Tier Model

1. **Lightweight references in conversation**  
   Conversation memory stores **references** (e.g. `artifact_id`, filename, content type, size), not raw payloads. Used for UI display, context assembly, and policy (e.g. which artifacts to include in the next turn).

2. **Raw payloads in the Artifact Store**  
   The **Artifact Store** holds the actual bytes (or structured payloads). Access is gated by tenant/principal and optional encryption-at-rest. Implementations include `RedisArtifactStore` (still supported) and `S3ArtifactStore` for S3-compatible backends — **SeaweedFS** in local distributed compose, **AWS S3** on EC2.

## Artifact Kinds

Artifacts are classified by **kind** (see `ArtifactKind` in `motet.core.artifacts.types`):

| Kind | Description |
|------|-------------|
| `user_upload` | Raw user-uploaded file (PDF, image, etc.) |
| `tool_artifact` | Raw tool output stored for transcript reconstruction |
| `derived_text` | Extracted text from a document (e.g. PDF/DOCX) |
| `derived_ocr` | OCR text from images or scanned PDFs |
| `derived_page_image` | Rendered page image (e.g. for vision models) |
| `derived_image_*` | Image variants (thumb, base, detail, ROI) for provider/model needs |
| `unknown` | Fallback when kind is not set |

**User uploads** can have **derived artifacts** (e.g. `extracted_text`, `page_images`) produced by the derivation pipeline and linked via `source_artifact_id` / metadata.

Artifact metadata is a scoped JSON bag for client-owned enrichment such as filenames, conversation links, source-system identifiers, and `artifact_tags`. Tags are normalized non-empty strings. The REST metadata patch endpoint appends new `artifact_tags` by default so post-upload enrichment can add labels without losing existing tags.

## User File Uploads

- **Upload flow**: Client uploads a file → API stores raw bytes in the Artifact Store → API returns `artifact_id` and metadata. A lightweight **upload reference** (artifact_id, filename, content_type, size, optional derived IDs) is associated with the conversation turn (e.g. in memory).
- **Rule**: Never inline raw base64 file bytes into the text prompt. Images are attached via the provider’s multimodal schema; documents are included via **extracted text** (and optionally page images for vision models).
- **Provider-agnostic content parts**: Internally, messages can use **content parts** (e.g. `{"type": "text", "text": "..."}`, `{"type": "image", "artifact_id": "...", "content_type": "image/png"}`). Provider adapters convert these to OpenAI/Claude/Gemini-specific message shapes.

## Tool Outputs and Transcripts

- **ToolInvocation** records (in memory) describe each tool call and outcome; they reference **ToolArtifact**s by `artifact_id` for large/raw payloads.
- **Schema-correct reconstruction**: When preparing context for a new turn, the system reconstructs valid tool-call transcripts (e.g. `assistant` message with `tool_calls` followed by `tool` messages with matching `tool_call_id`) from ToolInvocation records, instead of injecting orphan tool outputs.
- Tool transcript rendering is provider-specific (see `motet.core.tools.rendering.*`); artifact payloads are fetched only when needed and rendered in a provider-safe way.

## Artifacts API

REST API for listing, uploading, and downloading artifacts (see `interfaces/api/v1/artifacts.py`).

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/artifacts` | List artifacts (optional filters: `kind`, `conversation_id`; pagination: `limit`, `offset`) |
| POST | `/api/v1/artifacts` | Upload a file; returns `artifact_id`, `filename`, `content_type`, `bytes`, `kind` |
| GET | `/api/v1/artifacts/indexing-status` | Bulk derived-text chunk indexing status: repeat query param `artifact_id` (max 80); includes chunk counts, derivation state, index health, and per-artifact eligibility |
| GET | `/api/v1/artifacts/{id}/metadata` | Get artifact metadata |
| PATCH | `/api/v1/artifacts/{id}/metadata` | Merge custom metadata and normalized `artifact_tags`; tags append by default unless `merge_artifact_tags=false` |
| GET | `/api/v1/artifacts/{id}/download` | Download artifact bytes (access control by tenant/principal) |
| GET | `/api/v1/artifacts/{id}/preview` | Preview (e.g. image) when supported |
| POST | `/api/v1/artifacts/{id}/reindex` | Queue preparation/indexing for the resolved source; pass `wait=true` for synchronous debug/test execution |
| GET | `/api/v1/artifacts/reindex-tasks/{task_id}` | Read queued/running/completed reindex task status |
| PATCH | `/api/v1/artifacts/{id}/indexing-policy` | Enable or disable durable indexing eligibility and optional disabled strategies for an artifact source |
| GET | `/api/v1/artifacts/preparation/strategies` | List registered preparation strategies |
| POST | `/api/v1/artifacts/preparation/plan` | Dry-run preparation strategy selection |
| DELETE | `/api/v1/artifacts/{id}` | Delete artifact |

All endpoints require authentication (`get_current_principal`). Artifacts are scoped by tenant/principal/motet.

Example metadata patch request:

```json
{
  "metadata": {"source": "memo", "memo_asset_id": "draft_123"},
  "artifact_tags": ["jersey", "signed"],
  "merge_artifact_tags": true
}
```

Preparation strategies are selected by artifact type. DOCX uploads use a structure-aware strategy that preserves heading paths and table chunks; the generic office-document strategy remains available for PDF, PPTX, ODT, RTF, and explicit fallback runs.

## Using the Artifact Store in Code

```python
from motet.core.artifacts import get_artifact_store, ArtifactKind

store = get_artifact_store()

# Store an artifact (e.g. after tool execution or upload)
artifact_id = store.put(
    payload=raw_bytes_or_dict,
    content_type="application/octet-stream",
    kind=ArtifactKind.USER_UPLOAD,
    tenant_id=tenant_id,
    principal_id=principal_id,
)

# Retrieve when building context (gated by context)
payload = store.get(artifact_id, tenant_id=tenant_id, principal_id=principal_id)
```

Orchestration and derivation use the same store; see `motet.core.commands.builtin.artifacts` and `motet.core.media.derivation_service` for upload handling and derived-artifact creation.

## Multimodal Context Injection

- **prepare_context** (or helpers it calls) decides which artifacts to include for the next turn (policy, model capabilities, token/image budgets).
- Content is represented as **provider-agnostic content parts** (text + image/artifact references). The **provider layer** (e.g. `motet.core.models.rendering.*` or provider adapters) converts these to provider-native message schemas (OpenAI, Anthropic, Gemini).
- Rule: never put base64 image data into a text field; use the provider’s image/multimodal part types, populated from artifact bytes at render time.

## Key Files and Modules

- **Types and protocol**: `motet.core.artifacts.types` (ArtifactKind, ArtifactMetadata), `motet.core.artifacts.protocol` (ArtifactStoreProtocol)
- **Store implementation**: `motet.core.artifacts.redis_artifact_store`, `motet.core.artifacts.scoped_store`
- **API**: `motet.interfaces.api.v1.artifacts`
- **Orchestration/commands**: `motet.core.commands.builtin.artifacts`, `motet.core.commands.builtin.derivation`
- **Media/derivation**: `motet.core.media.derivation_service`, `motet.core.media.text_extraction`
- **Tool transcripts**: `motet.core.tools.transcript_service`, `motet.core.tools.rendering.*`

## Next Steps

- **[Memory Management](./20-memory-management.md)** – Conversation memory and references
- **[Tool Ecosystem](./21-tool-ecosystem.md)** – Tool execution and tool outputs
- **[API Reference](./28-api-reference.md)** – Artifacts API summary
## Navigation

- **[← Memory Management](./20-memory-management.md)** – Memory tiers and operations
- **[Tool Ecosystem →](./21-tool-ecosystem.md)** – Tools and execution
- **[Documentation Home](./00-landing-page.md)** – Main documentation hub

---

**Last Updated**: 2026-05-19
