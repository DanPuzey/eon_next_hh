#!/usr/bin/env bash
# Home Assistant does not run on native Windows; run the test suite under WSL with a Linux-side venv.
set -euo pipefail
cd "$(dirname "$0")/.."
export UV_PROJECT_ENVIRONMENT="$HOME/.venvs/eon_next_hh" UV_LINK_MODE=copy
uv=(uvx uv@0.13.0)
"${uv[@]}" sync --quiet -p 3.14
exec "${uv[@]}" run --quiet -p 3.14 pytest "$@"
