#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../../.." && pwd)"
# shellcheck source=../../../config/experiment/environment/versions.env
source "$REPO_ROOT/config/experiment/environment/versions.env"

KIND_BIN="${KIND_BIN:-kind}"
KUBECTL_BIN="${KUBECTL_BIN:-kubectl}"
KIND_CONTEXT="kind-${KIND_CLUSTER_NAME}"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

require_tools() {
  command -v "$KIND_BIN" >/dev/null 2>&1 || fail "kind não encontrado: $KIND_BIN"
  command -v "$KUBECTL_BIN" >/dev/null 2>&1 || fail "kubectl não encontrado: $KUBECTL_BIN"
  command -v docker >/dev/null 2>&1 || fail "docker não encontrado"
  command -v python3 >/dev/null 2>&1 || fail "python3 não encontrado"
  command -v curl >/dev/null 2>&1 || fail "curl não encontrado"

  local kind_output kubectl_version docker_server
  kind_output="$("$KIND_BIN" version)"
  [[ "$kind_output" == *"kind ${KIND_VERSION}"* ]] || \
    fail "versão Kind incompatível; esperado ${KIND_VERSION}; obtido: $kind_output"

  kubectl_version="$("$KUBECTL_BIN" version --client -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["clientVersion"]["gitVersion"])')"
  [[ "$kubectl_version" == "$KUBECTL_VERSION" ]] || \
    fail "versão kubectl incompatível; esperado ${KUBECTL_VERSION}; obtido: $kubectl_version"

  docker_server="$(docker version --format '{{.Server.Version}}')"
  [[ "$docker_server" == "$DOCKER_SERVER_VERSION" ]] || \
    fail "versão Docker Engine incompatível; esperado ${DOCKER_SERVER_VERSION}; obtido: $docker_server"
  docker info >/dev/null 2>&1 || fail "Docker daemon indisponível"
}

require_cluster() {
  "$KIND_BIN" get clusters | grep -Fxq "$KIND_CLUSTER_NAME" || \
    fail "cluster Kind ausente: $KIND_CLUSTER_NAME"
  [[ "$("$KUBECTL_BIN" config current-context)" == "$KIND_CONTEXT" ]] || \
    fail "contexto kubectl não é $KIND_CONTEXT"
}

require_exclusive_kind_runtime() {
  local running_nodes other_clusters
  running_nodes="$(docker ps --filter label=io.x-k8s.kind.cluster \
    --format '{{.Label "io.x-k8s.kind.cluster"}}')" || \
    fail "não foi possível verificar clusters Kind ativos no Docker"

  other_clusters="$(printf '%s\n' "$running_nodes" \
    | sed '/^[[:space:]]*$/d' \
    | sort -u \
    | while IFS= read -r cluster; do
        [[ "$cluster" == "$KIND_CLUSTER_NAME" ]] || printf '%s\n' "$cluster"
      done)"

  [[ -z "$other_clusters" ]] || fail "Outro cluster Kind está ativo: ${other_clusters//$'\n'/, }. O ambiente experiment-v2 exige runtime Kind exclusivo devido ao limite compartilhado de instâncias inotify deste WSL. Pare manualmente o outro cluster antes de continuar; nenhum cluster será parado automaticamente."
}

sut_deployments=(
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
  redis-cart
)
