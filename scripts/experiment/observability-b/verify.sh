#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../../.." && pwd)"
# shellcheck source=../environment/common.sh
source "$REPO_ROOT/scripts/experiment/environment/common.sh"

require_tools
require_cluster
require_exclusive_kind_runtime

"$KUBECTL_BIN" --context "$KIND_CONTEXT" wait \
  --for=condition=Available deployment --all \
  --namespace observability-b --timeout=5m

if "$KUBECTL_BIN" --context "$KIND_CONTEXT" get daemonset/promtail-b \
  --namespace observability-b >/dev/null 2>&1; then
  fail "promtail-b must not collect Scenario B application stdout; B application logs are OTLP-only"
fi

"$KUBECTL_BIN" --context "$KIND_CONTEXT" get deployments,daemonsets,pods \
  --namespace observability-b -o wide
