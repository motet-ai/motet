"""
Motet - Media Processing & Attachments

## Overview

The `motet.core.media` package implements **User File Uploads as Artifacts**.
It provides the foundation for handling user-uploaded files, processing them (text extraction, vision OCR),
and injecting them into the LLM context.

## Core Concepts

### 1. Two-Tier Persistence

Media files (user uploads, tool artifacts) are stored in two places:
1. **Artifact Store**: The raw file bytes (and derived versions like extracted text) are stored as Artifacts (Redis/S3).
2. **Conversation Memory**: A lightweight reference (`UploadAttachment`) is stored in the conversation turn metadata.

### 2. Derivation Pipeline

When a media file is uploaded or produced by a tool, background commands run to create derived artifacts:

**Text Extraction** (`derive_upload_text`):
- Extract text from PDFs, DOCX, XLSX, TXT, JSON, XML.
- Stored as `DERIVED_TEXT` artifacts.
- For PDFs, this command additionally:
 - Creates per-page images (`DERIVED_PAGE_IMAGE`) via `derive_pdf_page_images`
 - Runs per-page vision OCR via `ocr_image_page` (parallelized using `motet.apply` / MapCommand)
 - Combines text layer + OCR into the final `DERIVED_TEXT`

**Video Processing** (`derive_video_visuals` + `derive_video_transcript`, /):
- Triggered automatically for `video/*` uploads on workers with `MEDIA_PROCESSING` (ffmpeg/ffprobe present).
- The two commands are dispatched **in parallel** from `create_artifact`: visuals extract a poster frame (`DERIVED_VIDEO_POSTER`) and scene/interval keyframes (`DERIVED_VIDEO_KEYFRAME`); the transcript command extracts audio and produces an optional transcript (`DERIVED_VIDEO_TRANSCRIPT`) via a pluggable backend (`MOTET_VIDEO_TRANSCRIPTION_BACKEND=none|whisper_cli|openai_api`).
- The `openai_api` backend posts extracted audio to OpenAI `/v1/audio/transcriptions` (chunked under the 25 MB upload limit, segment timestamps preserved); it skips with a structured log when `OPENAI_API_KEY` is missing. Selecting it is the deployment's explicit authorization for audio egress.
- **Speaker diarization** (`openai_api` only): set `MOTET_VIDEO_TRANSCRIPTION_MODEL=gpt-4o-transcribe-diarize` and the backend requests `diarized_json`, populating `TranscriptSegment.speaker`; rendered transcripts emit `[start-end] Speaker: text` lines so RAG chunks and context injection carry who-said-what for free. Labels are assigned per request — videos long enough to span multiple upload chunks log `video_transcription_diarization_chunked` because labels are not guaranteed consistent across chunks.
- Keyframes are ordinary JPEG artifacts — vision models consume them via existing image paths; transcripts index through artifact RAG. Poster and keyframe extraction downscales to a 1024px longest side at derivation time: these frames serve as LLM prompt images, and native 1080p/4K frames produced oversized provider requests. The original video remains the archival source.
- Browser playback uses HTTP `Range` on `GET /api/v1/artifacts/{id}/download` and `/preview`.
- For `<video>` elements (which cannot send auth headers), clients mint a short-lived token via
 `POST /api/v1/artifacts/{id}/playback-token` and stream inline from
 `GET /api/v1/artifacts/{id}/stream?token=...` (Range-capable; see `core/artifacts/playback_tokens.py`).

**Image Processing** (`derive_upload_image`):
- Generate optimized image derivations for LLM vision models:
 - **thumb**: 512px max side (UI previews)
 - **base**: 1600px max side (default for LLM input, cost/quality balance)
 - **detail**: 2048px max side (high-res for text extraction, OCR tasks)
- Stored as `DERIVED_IMAGE_THUMB`, `DERIVED_IMAGE_BASE`, `DERIVED_IMAGE_DETAIL` artifacts.
- All derivations are generated eagerly on upload for simplicity and immediate availability.

**Office Embedded Images** (`derive_office_embedded_images`):
- Extracts embedded image payloads from DOCX and PPTX OOXML packages.
- Stores each image as a `DERIVED_EMBEDDED_IMAGE` artifact linked to the source document.
- Preserves relationship/location metadata such as slide number, package path, relationship ID,
 image name, and alt text when present.
- Adds heuristic role/relevance metadata (`embedded_image_role`, `embedded_image_relevance_score`,
 `embedded_image_should_ocr`) using dimensions, filename/path, image name, and alt text.
- Dispatches `derive_upload_image` for each extracted image so thumb/base image derivations are
 available for UI display and future multimodal context selection.
- Dispatches `ocr_embedded_image` for each extracted image, stores OCR text as `DERIVED_OCR`,
 and indexes that text into artifact RAG against the original office document unless the image
 is classified as likely decorative/logo/icon content.

The derived content is stored as new artifacts linked to the source upload.

### 3. Context Injection

During `agent_turn`, the `prepare_context` command:
- Retrieves conversation history.
- Scans messages for `attachments`.
- **Documents**: Fetches extracted text from the artifact store and injects it into the message content.
- **Images**:
 - Selects appropriate derivation (base/detail) based on task context.
 - Defaults to "base" for general vision tasks (cost/quality balance).
 - Escalates to "detail" for text extraction tasks (keywords: "read", "extract", "ocr", "invoice", etc.).
 - Falls back to original artifact if no derivation exists.
 - Passes selected image artifact reference to the model provider (e.g. OpenAI) for multimodal processing.

## Usage

### Uploading a File

```python
from motet.interfaces.api.v1.artifacts import upload_artifact
# POST /api/v1/artifacts
```

### Retrieving an Artifact

```python
from motet.core.artifacts import get_artifact_store
store = get_artifact_store
data = store.get(artifact_id, tenant_id="...")
```

### Running Extraction/Processing Manually

```python
from motet.core.media.derivation_service import derive_text_artifact, derive_image_artifacts
from motet.core.commands.builtin.derivation import derive_pdf_page_images, ocr_image_page
from motet.core.commands.command_data_classes import DerivePdfPageImagesData, OCRImagePageData

# Text extraction
result = derive_text_artifact(source_id, tenant_id)

# Image derivations (generate thumb + base)
result = derive_image_artifacts(source_artifact_id=image_id,
 tenant_id=tenant_id,
 derivation_names=["thumb", "base"])

# PDF page images (stored as artifacts for reuse/display) + OCR are handled by commands:
# - derive_pdf_page_images(DerivePdfPageImagesData(...))
# - ocr_image_page(OCRImagePageData(...))
```

## Supported Formats

- **Text Extraction**: PDF, DOCX, PPTX, XLSX, TXT, JSON, XML
- **Embedded Image Extraction + OCR**: DOCX and PPTX
- **Image Input**: PNG, JPG, WEBP, GIF (supported by vision models)
- **Optional AVIF conversion**: AVIF uploads can be normalized to model-ready
 JPEG bytes when `MOTET_ENABLE_AVIF_CONVERSION=1` is set in the API/worker
 runtime. Decode uses Pillow 11.3+ native AVIF (libavif/dav1d in the wheel).
- **Optional HEIC/HEIF conversion**: Off by default. Motet does not ship
 `pillow-heif`. If an operator installs that package and sets
 `MOTET_ENABLE_HEIC_HEIF_CONVERSION=1`, HEIC/HEIF uploads can be normalized
 to JPEG. Review that package's wheel licenses before doing so; official
 wheels vendor HEVC codecs under GPL/LGPL.

AVIF conversion does not use `pillow-heif`. Enabling AVIF does not register
or decode HEIC.

## Architecture

- `types.py`: `UploadAttachment` model.
- `text_extraction.py`: Low-level text extraction logic (PDF, DOCX, XLSX).
- `office_embedded_images.py`: OOXML embedded-image extraction for DOCX/PPTX.
- `image_processing.py`: Image resizing and processing utilities (Pillow-based).
- `derivation_policy.py`: Policy logic for selecting image derivations (base/detail) based on task context.
- `derivation_service.py`: Orchestration of extraction/processing and storage.
- `..motet/core/commands/builtin/derivation.py`: Distributed derivation commands (async via dispatch).
"""

