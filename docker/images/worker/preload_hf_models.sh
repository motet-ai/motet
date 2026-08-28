#!/usr/bin/env bash
# Pre-download NLTK corpora used by keyword-style helpers into the worker image.
#
# After ADR-0107 M3, text embedding models are not baked into workers; workers use
# the sibling embedding service (or an opt-in in-process stack via extra deps).
set -euo pipefail

echo "Preloading NLTK corpora (wordnet, omw-1.4)..."
python3 -c "
import nltk
nltk.download('wordnet', quiet=True)
nltk.download('omw-1.4', quiet=True)
print('NLTK corpora ready.')
"
echo "NLTK preload complete."
