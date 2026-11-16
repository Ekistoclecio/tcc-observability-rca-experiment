#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
kind_bin="${KIND_BIN:-kind}"
kubectl_bin="${KUBECTL_BIN:-kubectl}"
expected_kind="v0.33.0"
cluster_name="tcc-baseline"
git_revision="$(git -C "${repo_root}" rev-parse HEAD)"
services=(
  frontend checkoutservice cartservice productcatalogservice currencyservice
  paymentservice shippingservice emailservice recommendationservice adservice
)

command -v docker >/dev/null || { echo 'Docker is required' >&2; exit 2; }
command -v "${kubectl_bin}" >/dev/null || { echo 'kubectl v1.35.x is required (set KUBECTL_BIN to its executable)' >&2; exit 2; }
command -v "${kind_bin}" >/dev/null || { echo 'Kind v0.33.0 is required (set KIND_BIN to its executable)' >&2; exit 2; }
docker info >/dev/null

if [[ -n "$(git -C "${repo_root}" status --porcelain)" ]]; then
  echo "Refusing to create a provenance-valid cluster from a dirty working tree." >&2
  exit 6
fi

actual_kind="$("${kind_bin}" version | sed -n '1s/^kind \(v[^ ]*\).*/\1/p')"
if [[ "${actual_kind}" != "${expected_kind}" ]]; then
  echo "Expected Kind ${expected_kind}, found ${actual_kind:-unknown}." >&2
  exit 3
fi
actual_kubectl="$("${kubectl_bin}" version --client -o json | python3 -c 'import json, sys; print(json.load(sys.stdin)["clientVersion"]["gitVersion"])')"
if [[ "${actual_kubectl}" != v1.35.* ]]; then
  echo "Expected kubectl v1.35.x to match the cluster, found ${actual_kubectl:-unknown}." >&2
  exit 3
fi

if "${kind_bin}" get clusters | grep -Fxq "${cluster_name}"; then
  echo "Kind cluster ${cluster_name} already exists; delete it explicitly before recreation." >&2
  exit 4
fi

images=()
for service in "${services[@]}"; do
  image="${service}:tcc-${git_revision}"
  if ! docker image inspect "${image}" >/dev/null 2>&1; then
    echo "Required local image ${image} is missing." >&2
    echo "Build application images sequentially, then rerun this script:" >&2
    echo "  TCC_LOAD_IMAGES=0 scripts/experiment/build-application-images.sh" >&2
    exit 5
  fi
  image_revision="$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "${image}")"
  if [[ "${image_revision}" != "${git_revision}" ]]; then
    echo "Image ${image} has revision ${image_revision:-<missing>}, expected ${git_revision}." >&2
    exit 7
  fi
  images+=("${image}")
done

"${kind_bin}" create cluster \
  --config "${repo_root}/deployments/experiment/kind/cluster.yaml" \
  --wait 5m
"${kind_bin}" load docker-image --name "${cluster_name}" "${images[@]}"

"${kubectl_bin}" --context "kind-${cluster_name}" wait --for=condition=Ready node --all --timeout=180s
"${kubectl_bin}" --context "kind-${cluster_name}" get nodes -o wide
echo "Created ${cluster_name} using the pinned Kubernetes v1.35.8 Kind node image and loaded 10 images for revision ${git_revision}."
