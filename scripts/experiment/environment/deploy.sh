#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

require_tools
require_cluster
"$KUBECTL_BIN" --context "$KIND_CONTEXT" create namespace "$SUT_NAMESPACE" --dry-run=client -o yaml \
  | "$KUBECTL_BIN" --context "$KIND_CONTEXT" apply -f -
"$KUBECTL_BIN" kustomize --load-restrictor=LoadRestrictionsNone \
  "$REPO_ROOT/kustomize/experiment-v2" \
  | "$KUBECTL_BIN" --context "$KIND_CONTEXT" apply -f -
"$KUBECTL_BIN" --context "$KIND_CONTEXT" wait \
  --for=condition=Available deployment --all \
  --namespace "$SUT_NAMESPACE" --timeout=5m
