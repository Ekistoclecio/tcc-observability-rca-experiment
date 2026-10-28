#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
venv_path="${repo_root}/.venv-tcc-workload"

python3 -m venv "${venv_path}"
"${venv_path}/bin/python" -m pip install --disable-pip-version-check -r \
  "${repo_root}/src/loadgenerator/experiment/requirements.txt"
printf 'Workload environment ready: %s\n' "${venv_path}"
