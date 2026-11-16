#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

require_tools
require_exclusive_kind_runtime
if "$KIND_BIN" get clusters | grep -Fxq "$KIND_CLUSTER_NAME"; then
  fail "cluster $KIND_CLUSTER_NAME já existe; create não reutiliza nem apaga clusters"
fi

"$KIND_BIN" create cluster \
  --name "$KIND_CLUSTER_NAME" \
  --config "$REPO_ROOT/config/experiment/environment/kind-cluster.yaml" \
  --wait 5m
"$KUBECTL_BIN" --context "$KIND_CONTEXT" cluster-info
