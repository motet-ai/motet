#!/usr/bin/env bash
set -euo pipefail

# Microsoft Playwright MCP over stdio (cutover from ExecuteAutomation).
# @playwright/mcp 0.0.x --browser accepts chrome|firefox|webkit|msedge (not
# "chromium"), and defaults to chrome-for-testing which is not in the base
# image. Point at the Chromium shipped in mcr.microsoft.com/playwright.
BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/ms-playwright}"
# Playwright base images have used chrome-linux/ and chrome-linux64/ across versions.
CHROME_BIN="$(
  find "$BROWSERS_PATH" \
    \( -path '*/chrome-linux64/chrome' -o -path '*/chrome-linux/chrome' \) \
    -type f 2>/dev/null | head -1 || true
)"
if [[ -z "${CHROME_BIN}" ]]; then
  echo "playwright-mcp: no chrome binary under ${BROWSERS_PATH}" >&2
  exit 1
fi

# Default browser context: desktop Chrome UA + viewport (matches core.http_get_browser).
# Sites like cnn.com return a bare "Unknown Error" page to Playwright's default
# HeadlessChrome UA; overriding via --config fixes that. Callers may still pass
# their own --config (first wins for duplicate flags is undefined — we only add
# the default when none was supplied).
DEFAULT_CONFIG="${PLAYWRIGHT_MCP_CONFIG:-/etc/playwright-mcp/config.json}"
EXTRA_ARGS=()
has_config=false
for arg in "$@"; do
  if [[ "$arg" == "--config" || "$arg" == --config=* ]]; then
    has_config=true
    break
  fi
done
if [[ "$has_config" == false && -f "$DEFAULT_CONFIG" ]]; then
  EXTRA_ARGS+=(--config "$DEFAULT_CONFIG")
fi

# Image responses stay enabled so browser_take_screenshot returns MCP image
# content (base64) for Motet vision / ui_verify — do not pass --image-responses omit.
exec playwright-mcp \
  --headless \
  --executable-path "${CHROME_BIN}" \
  --no-sandbox \
  --image-responses allow \
  "${EXTRA_ARGS[@]}" \
  "$@"
