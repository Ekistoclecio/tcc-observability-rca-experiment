#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

command -v "$KIND_BIN" >/dev/null 2>&1 || fail "kind não encontrado: $KIND_BIN"
kind_output="$("$KIND_BIN" version)"
[[ "$kind_output" == *"kind ${KIND_VERSION}"* ]] || \
  fail "versão Kind incompatível; esperado ${KIND_VERSION}; obtido: $kind_output"
if "$KIND_BIN" get clusters | grep -Fxq "$KIND_CLUSTER_NAME"; then
  "$KIND_BIN" delete cluster --name "$KIND_CLUSTER_NAME"
else
  printf 'Cluster %s não existe; nada a remover.\n' "$KIND_CLUSTER_NAME"
fi
