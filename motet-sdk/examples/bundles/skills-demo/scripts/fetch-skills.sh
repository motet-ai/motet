#!/usr/bin/env bash
# Fetch Anthropic document skills into this example bundle (local only).
#
# Motet does NOT redistribute proprietary / source-available document skills
# (pdf, docx, pptx, xlsx). This script sparse-clones them from anthropics/skills
# into skills/ (gitignored). Read each skill's LICENSE.txt before use.
#
# Usage (from repo root or this directory):
#   ./motet-sdk/examples/bundles/skills-demo/scripts/fetch-skills.sh
#   ./scripts/fetch-skills.sh pdf            # subset
#   ./scripts/fetch-skills.sh --ref main
#
# Then redeploy:
#   motet-cli deploy dir-deploy motet-sdk/examples/bundles/skills-demo
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${BUNDLE_DIR}/../../../.." && pwd)"
VENDOR="${REPO_ROOT}/scripts/vendor_public_agent_skills.sh"
BUNDLE_ID="skills-demo"

# Default: proprietary document skills only.
DEFAULT_SLUGS=(pdf docx pptx xlsx)
REF_ARGS=()
SLUGS=()

usage() {
  sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage 0 ;;
    --ref) REF_ARGS=(--ref "$2"); shift 2 ;;
    *) SLUGS+=("$1"); shift ;;
  esac
done

if [[ ${#SLUGS[@]} -eq 0 ]]; then
  SLUGS=("${DEFAULT_SLUGS[@]}")
fi

if [[ ! -x "${VENDOR}" && ! -f "${VENDOR}" ]]; then
  echo "error: vendor script not found at ${VENDOR}" >&2
  exit 1
fi

echo "=== License reminder ==="
echo "Document skills are Anthropic source-available / proprietary."
echo "They are fetched into gitignored paths under ${BUNDLE_DIR}/skills/."
echo "Do NOT commit them into Motet or any Motet public export."
echo "Each folder's LICENSE.txt applies. Not legal advice."
echo ""

vendor_args=(
  --merge
  --preserve-docs
  --preserve-agents
  --out "${BUNDLE_DIR}"
  --bundle-id "${BUNDLE_ID}"
)
if [[ ${#REF_ARGS[@]} -gt 0 ]]; then
  vendor_args+=("${REF_ARGS[@]}")
fi
vendor_args+=("${SLUGS[@]}")

bash "${VENDOR}" "${vendor_args[@]}"

# Sync agent skill_ids to every skills/*/SKILL.md present (committed + fetched).
AGENTS="${BUNDLE_DIR}/agents/agents.yaml"
if [[ ! -f "${AGENTS}" ]]; then
  echo "error: missing ${AGENTS}" >&2
  exit 1
fi

python3 - "${BUNDLE_DIR}" "${BUNDLE_ID}" <<'PY'
from __future__ import annotations

import re
import sys
from pathlib import Path

bundle = Path(sys.argv[1])
bundle_id = sys.argv[2]
skills_root = bundle / "skills"
slugs = sorted(
    p.name
    for p in skills_root.iterdir()
    if p.is_dir() and (p / "SKILL.md").is_file()
)
if not slugs:
    raise SystemExit(f"error: no skills with SKILL.md under {skills_root}")

agents_path = bundle / "agents" / "agents.yaml"
text = agents_path.read_text(encoding="utf-8")
block = "\n".join(f'      - "{bundle_id}.{s}"' for s in slugs) + "\n"
updated, n = re.subn(
    r"(skill_ids:\n)(?:[ \t]*-[ \t]*.*\n)+",
    rf"\1{block}",
    text,
    count=1,
)
if n != 1:
    raise SystemExit("error: could not locate skill_ids block in agents/agents.yaml")
agents_path.write_text(updated, encoding="utf-8")
print(f"Updated agents/agents.yaml skill_ids ({len(slugs)} skills): {', '.join(slugs)}")
PY

echo ""
echo "Fetched into ${BUNDLE_DIR}/skills/: ${SLUGS[*]}"
echo "Redeploy with:"
echo "  motet-cli deploy dir-deploy ${BUNDLE_DIR#"${REPO_ROOT}/"}"
