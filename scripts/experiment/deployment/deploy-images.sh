#!/usr/bin/env bash
set -euo pipefail

DEPLOY_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$DEPLOY_DIR/../../.." && pwd)"
# shellcheck source=../../../config/experiment/environment/versions.env
source "$REPO_ROOT/config/experiment/environment/versions.env"
# shellcheck source=../environment/common.sh
source "$REPO_ROOT/scripts/experiment/environment/common.sh"

require_tools
require_cluster
require_exclusive_kind_runtime

revision="$(git -C "$REPO_ROOT" rev-parse HEAD)"
"$DEPLOY_DIR/build-images.sh"

services=(
  adservice
  cartservice
  checkoutservice
  currencyservice
  emailservice
  frontend
  paymentservice
  productcatalogservice
  recommendationservice
  shippingservice
)
images=()
for service in "${services[@]}"; do
  image="$EXPERIMENT_IMAGE_PREFIX/$service:$revision"
  actual_revision="$(docker image inspect "$image" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')"
  [[ "$actual_revision" == "$revision" ]] || {
    printf 'ERROR: revision OCI incorreta em %s: %s\n' "$image" "$actual_revision" >&2
    exit 1
  }
  images+=("$image")
done

"$KIND_BIN" load docker-image --name "$KIND_CLUSTER_NAME" "${images[@]}"
node_names="$(docker ps --filter "label=io.x-k8s.kind.cluster=$KIND_CLUSTER_NAME" --format '{{.Names}}')"
[[ "$(printf '%s\n' "$node_names" | sed '/^[[:space:]]*$/d' | wc -l)" -eq 1 ]] || \
  fail "esperado exatamente um node Kind em execução: $node_names"
node_name="$(printf '%s\n' "$node_names" | sed '/^[[:space:]]*$/d')"
for image in "${images[@]}"; do
  docker exec "$node_name" crictl inspecti "$image" | python3 -c '
import json, sys
expected, revision = sys.argv[1:]
data = json.load(sys.stdin)
status = data.get("status", {})
labels = data.get("info", {}).get("imageSpec", {}).get("config", {}).get("Labels", {}) or {}
if status.get("id") != expected or labels.get("org.opencontainers.image.revision") != revision:
    raise SystemExit("Kind image verification failed for " + expected)
' "$(docker image inspect "$image" --format '{{.Id}}')" "$revision"
done

"$KUBECTL_BIN" --context "$KIND_CONTEXT" create namespace "$SUT_NAMESPACE" --dry-run=client -o yaml \
  | "$KUBECTL_BIN" --context "$KIND_CONTEXT" apply -f -
manifest="$("$KUBECTL_BIN" kustomize --load-restrictor=LoadRestrictionsNone "$REPO_ROOT/kustomize/experiment-v2")"
manifest="$(printf '%s\n' "$manifest" | sed "s/:EXPERIMENT_REVISION/:$revision/g")"
if grep -q 'EXPERIMENT_REVISION' <<<"$manifest"; then
  fail "manifesto ainda contém EXPERIMENT_REVISION não resolvido"
fi
"$KUBECTL_BIN" --context "$KIND_CONTEXT" apply -f - <<<"$manifest"

"$KUBECTL_BIN" --context "$KIND_CONTEXT" wait \
  --for=condition=Available deployment --all \
  --namespace "$SUT_NAMESPACE" --timeout=5m
python3 "$DEPLOY_DIR/verify_provenance.py" \
  --output "${TCC_PROVENANCE_OUTPUT:-${TMPDIR:-/tmp}/tcc-experiment-v2-deployment-provenance.json}"
