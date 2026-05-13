#!/usr/bin/env bash
# One-time setup: point this repo's git hooks at .githooks/ instead of
# the default .git/hooks/. Run once per fresh clone.
set -euo pipefail
cd "$(dirname "$0")/.."
git config core.hooksPath .githooks
echo "core.hooksPath set to .githooks"
