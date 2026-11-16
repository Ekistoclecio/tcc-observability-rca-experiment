#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../../.." && pwd)"
# shellcheck source=../environment/common.sh
source "$REPO_ROOT/scripts/experiment/environment/common.sh"

require_tools
require_cluster
require_exclusive_kind_runtime

"$KUBECTL_BIN" --context "$KIND_CONTEXT" apply \
  -k "$REPO_ROOT/kustomize/experiment-v2/observability-b"
