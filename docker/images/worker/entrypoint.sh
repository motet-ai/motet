#!/usr/bin/env bash
# Ensure Playwright Chromium exists in the mounted volume and CHROME_BIN symlink is valid.
# At runtime, /tmp/playwright-browsers is a volume that overrides the image content,
# so we must install into the volume and symlink on every start (idempotent).
set -euo pipefail

# core.worker_exec allowlisted root (0700); idempotent if image already created it.
install -d -m 700 /var/motet/worker-exec

PLAYWRIGHT_BROWSERS="${PLAYWRIGHT_BROWSERS_PATH:-/tmp/playwright-browsers}"

# Step 1: Install Chromium via Python Playwright (idempotent — skips if already present).
python -m playwright install chromium

# Step 2: Create compatibility symlinks so the Node.js Playwright bundled inside
# @playwright/mcp can find the same binary.
# Python Playwright installs e.g. chromium-1208 and chromium_headless_shell-1208.
# The Node.js Playwright (older bundled version) may look for chromium-1200 and
# chromium_headless_shell-1200. We symlink those expected paths to the actual ones.
# Playwright uses dashes in browsers.json names but underscores on disk (chromium_headless_shell).

# Find the actual installed chromium revision directories
ACTUAL_CHROMIUM=$(find "$PLAYWRIGHT_BROWSERS" -maxdepth 1 -type d -name 'chromium-[0-9]*' 2>/dev/null | sort -V | tail -1)
ACTUAL_HEADLESS=$(find "$PLAYWRIGHT_BROWSERS" -maxdepth 1 -type d -name 'chromium_headless_shell-[0-9]*' 2>/dev/null | sort -V | tail -1)

# Read the revision the Node.js Playwright expects from its cached browsers.json.
# Two browsers.json files may exist:
#   1. node_modules/playwright-core/browsers.json          ← used by the MCP server (want this one)
#   2. node_modules/@playwright/test/.../playwright-core/  ← nested dep with a different revision
# Exclude paths under @playwright/* to ensure we pick the top-level (MCP-server-facing) file.
NPX_CACHE="${npm_config_cache:-/root/.npm}/_npx"
EXPECTED_REV=""
if [ -d "$NPX_CACHE" ]; then
  BROWSERS_JSON=$(find "$NPX_CACHE" -name 'browsers.json' -path '*/playwright-core/browsers.json' \
    ! -path '*/@playwright/*' 2>/dev/null | head -1)
  if [ -n "$BROWSERS_JSON" ]; then
    EXPECTED_REV=$(node -e "
      var d=JSON.parse(require('fs').readFileSync('$BROWSERS_JSON','utf8'));
      var c=(d.browsers||[]).find(function(b){return b.name==='chromium';});
      if(c) process.stdout.write(String(c.revision));
    " 2>/dev/null || echo "")
    echo "Node.js Playwright browsers.json: $BROWSERS_JSON (expected chromium rev: ${EXPECTED_REV:-unknown})"
  fi
fi

if [ -n "$EXPECTED_REV" ] && [ -n "$ACTUAL_CHROMIUM" ]; then
  ACTUAL_CHROM_BASENAME=$(basename "$ACTUAL_CHROMIUM")
  EXPECTED_CHROM_DIR="$PLAYWRIGHT_BROWSERS/chromium-${EXPECTED_REV}"
  if [ ! -e "$EXPECTED_CHROM_DIR" ]; then
    ln -sf "$ACTUAL_CHROM_BASENAME" "$EXPECTED_CHROM_DIR"
    echo "✅ Chromium compat symlink: chromium-${EXPECTED_REV} -> $ACTUAL_CHROM_BASENAME"
  fi

  if [ -n "$ACTUAL_HEADLESS" ]; then
    ACTUAL_HEADLESS_BASENAME=$(basename "$ACTUAL_HEADLESS")
    EXPECTED_HEADLESS_DIR="$PLAYWRIGHT_BROWSERS/chromium_headless_shell-${EXPECTED_REV}"
    if [ ! -e "$EXPECTED_HEADLESS_DIR" ]; then
      ln -sf "$ACTUAL_HEADLESS_BASENAME" "$EXPECTED_HEADLESS_DIR"
      echo "✅ Headless-shell compat symlink: chromium_headless_shell-${EXPECTED_REV} -> $ACTUAL_HEADLESS_BASENAME"
    fi
  fi
fi

# Step 3: Symlink so CHROME_BIN/CHROME_PATH point at Playwright's Chromium.
PLAYWRIGHT_CHROME=$(find "$PLAYWRIGHT_BROWSERS" -path '*chromium*/chrome-linux/chrome' -type f 2>/dev/null | head -1)
if [ -z "$PLAYWRIGHT_CHROME" ]; then
  PLAYWRIGHT_CHROME=$(find "$PLAYWRIGHT_BROWSERS" -path '*chromium*/chrome-linux/headless_shell' -type f 2>/dev/null | head -1)
fi
if [ -n "$PLAYWRIGHT_CHROME" ]; then
  mkdir -p /usr/local/bin
  ln -sf "$PLAYWRIGHT_CHROME" /usr/local/bin/chromium
  echo "✅ Chromium ready: /usr/local/bin/chromium -> $PLAYWRIGHT_CHROME"
else
  echo "⚠️ No Chromium found under $PLAYWRIGHT_BROWSERS; browser tools may fail"
fi

exec "$@"
