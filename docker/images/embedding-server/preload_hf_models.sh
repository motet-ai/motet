#!/usr/bin/env bash
set -euo pipefail

# Pre-download the default text embedding model into the image so the sibling
# embedding server can start without runtime Hugging Face downloads.
export HF_HOME="${HF_HOME:-/opt/imf_hf_cache}"
export TRANSFORMERS_CACHE="${HF_HOME}/transformers"
export SENTENCE_TRANSFORMERS_HOME="${HF_HOME}/sentence_transformers"
mkdir -p "$HF_HOME" "$TRANSFORMERS_CACHE" "$SENTENCE_TRANSFORMERS_HOME"

MODEL_NAME="${MOTET_EMBEDDING_TEXT_MODEL:-sentence-transformers/all-MiniLM-L12-v2}"

echo "Preloading Hugging Face embedding model (${MODEL_NAME}) into ${HF_HOME}..."
python3 - <<PY
from sentence_transformers import SentenceTransformer

SentenceTransformer("${MODEL_NAME}")
print("Embedding model preloaded.")
PY
echo "HF embedding model preload complete."
