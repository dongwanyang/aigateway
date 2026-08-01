#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

for path in qa-report qa-reports reports/qa tests/qa-report test-results playwright-report screenshots; do
  rm -rf -- "$path"
done

if [[ -d docs ]]; then
  find docs -maxdepth 1 -type d -name 'qa-evidence-*' -prune -exec rm -rf -- {} +
fi

printf 'Removed generated QA screenshots and reports.\n'
