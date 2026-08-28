#!/usr/bin/env bash
# setup_repos.sh — initialise local git repos for bundle deployment testing.
#
# Each subdirectory in bundle source roots that contains a manifest.yaml is
# treated as a bundle source. This script creates a git repo under
# tests/bundles/.repos/<name>/ with the bundle content committed, ready for use
# with the local file:// URL scheme that deploy_bundle accepts.
#
# Usage:
#   cd /path/to/imf
#   bash tests/bundles/setup_repos.sh
#
# After running, use the printed file:// URLs as the repo_url argument to the
# deploy or validate endpoints:
#
#   curl -X POST http://localhost:8000/api/v1/deploy/validate \
#     -H "Content-Type: application/json" \
#     -d '{"repo_url":"file:///.../.repos/hello-world","ref":"main","path":"."}'
#
# The .repos/ directory is gitignored — do not commit it.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPOS_DIR="$SCRIPT_DIR/.repos"
SDK_EXAMPLES_DIR="$SCRIPT_DIR/../../motet-sdk/examples/bundles"
SOURCE_ROOTS=("$SCRIPT_DIR" "$SDK_EXAMPLES_DIR")

mkdir -p "$REPOS_DIR"

for source_root in "${SOURCE_ROOTS[@]}"; do
    [[ -d "$source_root" ]] || continue

    for bundle_src in "$source_root"/*/; do
        name="$(basename "$bundle_src")"
        manifest="$bundle_src/manifest.yaml"

        # Skip non-bundle directories (README, etc.)
        [[ -f "$manifest" ]] || continue

        repo_dir="$REPOS_DIR/$name"

        echo "→ Setting up repo for bundle: $name"

        # (Re-)create the repo from scratch so the script is idempotent.
        rm -rf "$repo_dir"
        mkdir -p "$repo_dir"

        # Copy bundle source into the repo root (the bundle path will be ".")
        cp -r "$bundle_src/." "$repo_dir/"

        # Remove any leftover __pycache__ directories before committing.
        find "$repo_dir" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

        git -C "$repo_dir" init -b main -q
        git -C "$repo_dir" config user.email "test@motet.local"
        git -C "$repo_dir" config user.name "Motet Test Setup"
        git -C "$repo_dir" add -A
        git -C "$repo_dir" commit -m "chore: initial bundle snapshot for testing" -q

        echo "   ✓ file://$repo_dir  (ref: main, path: .)"
    done
done

echo ""
echo "All bundle repos initialised under $REPOS_DIR"
echo ""
echo "Quick validate example (hello-world):"
echo "  curl -s -N -X POST http://localhost:8000/api/v1/deploy/validate \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"repo_url\":\"file://$REPOS_DIR/hello-world\",\"ref\":\"main\",\"path\":\".\"}'"
