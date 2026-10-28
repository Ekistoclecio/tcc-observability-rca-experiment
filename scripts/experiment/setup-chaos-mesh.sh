#!/usr/bin/env bash
set -euo pipefail

version="${CHAOS_MESH_VERSION:-2.8.6}"
allow_unsupported="${TCC_ALLOW_UNSUPPORTED_CHAOS_MESH:-0}"
node_version="$(kubectl get nodes -o jsonpath='{.items[0].status.nodeInfo.kubeletVersion}')"
minor="$(sed -E 's/^v[0-9]+\.([0-9]+).*/\1/' <<<"${node_version}")"
if [[ "${minor}" -lt 30 || "${minor}" -gt 35 ]]; then
  if [[ "${allow_unsupported}" != "1" ]]; then
    echo "Chaos Mesh ${version} is documented/tested with Kubernetes 1.30-1.35; found ${node_version}." >&2
    echo "For a local compatibility smoke only, rerun with TCC_ALLOW_UNSUPPORTED_CHAOS_MESH=1." >&2
    exit 3
  fi
  echo "WARNING: proceeding with an officially untested Kubernetes version: ${node_version}" >&2
fi

helm_bin="$(command -v helm || true)"
if [[ -z "${helm_bin}" ]]; then
  helm_version="3.17.3"
  case "$(uname -m)" in
    x86_64) helm_arch=amd64 ;;
    aarch64|arm64) helm_arch=arm64 ;;
    *) echo "unsupported Helm host architecture: $(uname -m)" >&2; exit 2 ;;
  esac
  tool_dir="${TMPDIR:-/tmp}/tcc-tools/helm-${helm_version}"
  mkdir -p "${tool_dir}"
  archive="${tool_dir}/helm-v${helm_version}-linux-${helm_arch}.tar.gz"
  base_url="https://get.helm.sh/helm-v${helm_version}-linux-${helm_arch}.tar.gz"
  curl -fsSL "${base_url}" -o "${archive}"
  curl -fsSL "${base_url}.sha256sum" -o "${archive}.sha256sum"
  (cd "${tool_dir}" && sha256sum -c "${archive}.sha256sum")
  tar -xzf "${archive}" -C "${tool_dir}"
  helm_bin="${tool_dir}/linux-${helm_arch}/helm"
fi

"${helm_bin}" repo add chaos-mesh https://charts.chaos-mesh.org --force-update
"${helm_bin}" repo update chaos-mesh
"${helm_bin}" upgrade --install chaos-mesh chaos-mesh/chaos-mesh \
  --namespace chaos-mesh --create-namespace --version "${version}" \
  --set chaosDaemon.runtime=containerd \
  --set chaosDaemon.socketPath=/run/containerd/containerd.sock \
  --set dashboard.create=false
kubectl rollout status deployment/chaos-controller-manager -n chaos-mesh --timeout=180s
kubectl rollout status daemonset/chaos-daemon -n chaos-mesh --timeout=180s
kubectl get crd stresschaos.chaos-mesh.org networkchaos.chaos-mesh.org
echo "Chaos Mesh ${version} is Ready. Kubernetes ${node_version}; officially tested minor: 30..35."
