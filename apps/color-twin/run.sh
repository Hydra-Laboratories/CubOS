#!/usr/bin/env bash
set -euo pipefail
app_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "$app_dir/../.." && pwd)"
if [[ -n "${CUBOS_TWIN_PYTHON:-}" ]]; then
  runtime="$CUBOS_TWIN_PYTHON"
elif [[ -x "$repo_dir/.venv-api/bin/python" ]]; then
  runtime="$repo_dir/.venv-api/bin/python"
elif [[ -x "$repo_dir/.venv/bin/python" ]]; then
  runtime="$repo_dir/.venv/bin/python"
else
  runtime=python3
fi
if [[ ! -f "$app_dir/dist/index.html" ]]; then
  echo "Build the UI first: npm ci --prefix apps/color-twin && npm run build --prefix apps/color-twin"
  exit 1
fi
export PYTHONPATH="$repo_dir/packages/core/src:$app_dir${PYTHONPATH:+:$PYTHONPATH}"
exec "$runtime" -m uvicorn sim.server:app --host 127.0.0.1 --port 8743
