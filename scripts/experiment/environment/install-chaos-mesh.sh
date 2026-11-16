#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

require_tools
require_cluster
require_exclusive_kind_runtime

node_version="$("$KUBECTL_BIN" --context "$KIND_CONTEXT" get nodes \
  -o jsonpath='{.items[0].status.nodeInfo.kubeletVersion}')"
[[ "$node_version" == "$KUBERNETES_VERSION" ]] || \
  fail "Kubernetes incompatível; esperado $KUBERNETES_VERSION; obtido $node_version"

helm_bin="${HELM_BIN:-$(command -v helm || true)}"
if [[ -z "$helm_bin" ]]; then
  case "$(uname -m)" in
    x86_64) helm_arch=amd64 ;;
    aarch64|arm64) helm_arch=arm64 ;;
    *) fail "arquitetura não suportada para Helm: $(uname -m)" ;;
  esac
  helm_dir="${TMPDIR:-/tmp}/tcc-experiment-v2-tools/helm-${HELM_VERSION}"
  helm_bin="$helm_dir/linux-${helm_arch}/helm"
  if [[ ! -x "$helm_bin" ]]; then
    mkdir -p "$helm_dir"
    archive="$helm_dir/helm-v${HELM_VERSION}-linux-${helm_arch}.tar.gz"
    curl --fail --silent --show-error --location \
      "https://get.helm.sh/helm-v${HELM_VERSION}-linux-${helm_arch}.tar.gz" \
      -o "$archive"
    curl --fail --silent --show-error --location \
      "https://get.helm.sh/helm-v${HELM_VERSION}-linux-${helm_arch}.tar.gz.sha256sum" \
      -o "$archive.sha256sum"
    (cd "$helm_dir" && sha256sum --check "$(basename "$archive").sha256sum")
    tar -xzf "$archive" -C "$helm_dir"
  fi
fi

helm_version="$("$helm_bin" version --template '{{.Version}}')"
[[ "$helm_version" == "v${HELM_VERSION}" ]] || \
  fail "versão Helm incompatível; esperado v${HELM_VERSION}; obtido $helm_version"

"$helm_bin" repo add chaos-mesh https://charts.chaos-mesh.org --force-update
"$helm_bin" repo update chaos-mesh
"$helm_bin" upgrade --install chaos-mesh chaos-mesh/chaos-mesh \
  --namespace chaos-mesh --create-namespace --version "$CHAOS_MESH_VERSION" \
  --set chaosDaemon.runtime=containerd \
  --set chaosDaemon.socketPath=/run/containerd/containerd.sock \
  --set dashboard.create=false
"$KUBECTL_BIN" --context "$KIND_CONTEXT" -n chaos-mesh rollout status \
  deployment/chaos-controller-manager --timeout=180s
"$KUBECTL_BIN" --context "$KIND_CONTEXT" -n chaos-mesh rollout status \
  daemonset/chaos-daemon --timeout=180s
"$KUBECTL_BIN" --context "$KIND_CONTEXT" get crd \
  stresschaos.chaos-mesh.org networkchaos.chaos-mesh.org
