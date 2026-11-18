#!/usr/bin/env bash
set -euo pipefail
REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"
if [[ -n "${PYTHON_BIN:-}" ]]; then
  :
elif [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
  PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
else
  PYTHON_BIN="python3"
fi
command -v "$PYTHON_BIN" >/dev/null 2>&1 || {
  printf 'ERROR: Python 3 not found: %s\n' "$PYTHON_BIN" >&2
  exit 127
}
exec "$PYTHON_BIN" -m scripts.experiment.cli "$@"
