#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cluster_name="${KIND_CLUSTER_NAME:-tcc-baseline}"
minimum_available_kib="${TCC_MIN_AVAILABLE_MEMORY_KIB:-2097152}"
build_memory="${TCC_BUILD_MEMORY:-1024m}"
build_cpu_quota="${TCC_BUILD_CPU_QUOTA:-50000}"
load_images="${TCC_LOAD_IMAGES:-1}"

all_services=(
  frontend
  checkoutservice
  productcatalogservice
  shippingservice
  cartservice
  currencyservice
  paymentservice
  emailservice
  recommendationservice
  adservice
)

if (( $# > 0 )); then
  services=("$@")
else
  services=("${all_services[@]}")
fi

declare -A contexts=(
  [frontend]="src/frontend"
  [checkoutservice]="src/checkoutservice"
  [productcatalogservice]="src/productcatalogservice"
  [shippingservice]="src/shippingservice"
  [cartservice]="src/cartservice/src"
  [currencyservice]="src/currencyservice"
  [paymentservice]="src/paymentservice"
  [emailservice]="src/emailservice"
  [recommendationservice]="src/recommendationservice"
  [adservice]="src/adservice"
)

for service in "${services[@]}"; do
  if [[ -z "${contexts[$service]:-}" ]]; then
    echo "Unknown service: ${service}" >&2
    exit 2
  fi
done

previous_load_replicas="$(kubectl get deployment loadgenerator -o jsonpath='{.spec.replicas}' 2>/dev/null || true)"
if [[ "${load_images}" == "1" && -z "${previous_load_replicas}" ]]; then
  echo "Cannot reach the cluster for image loading. Use TCC_LOAD_IMAGES=0 for an offline build." >&2
  exit 4
fi
restore_load() {
  if [[ -n "${previous_load_replicas}" ]]; then
    kubectl scale deployment/loadgenerator --replicas="${previous_load_replicas}" >/dev/null || true
  fi
}
trap restore_load EXIT

if [[ -n "${previous_load_replicas}" ]]; then
  kubectl scale deployment/loadgenerator --replicas=0 >/dev/null
fi

tagged_images=()
for service in "${services[@]}"; do
  available_kib="$(awk '/MemAvailable:/ {print $2}' /proc/meminfo)"
  if (( available_kib < minimum_available_kib )); then
    echo "Refusing to build ${service}: only ${available_kib} KiB available." >&2
    exit 3
  fi

  image="${service}:tcc-phase2"
  echo "Building ${image} sequentially (memory=${build_memory}, cpu-quota=${build_cpu_quota})"
  docker build \
    --resource "memory=${build_memory}" \
    --resource "cpu-quota=${build_cpu_quota}" \
    --load \
    --tag "${image}" \
    "${repo_root}/${contexts[$service]}"
  tagged_images+=("${image}")
done

if [[ "${load_images}" != "1" ]]; then
  echo "Built ${#tagged_images[@]} image(s); image loading was skipped."
  exit 0
fi

if command -v kind >/dev/null 2>&1; then
  kind load docker-image --name "${cluster_name}" "${tagged_images[@]}"
else
  control_plane="${cluster_name}-control-plane"
  docker image save "${tagged_images[@]}" |
    docker exec -i "${control_plane}" ctr -n k8s.io images import -
fi

echo "Built and loaded ${#tagged_images[@]} image(s) into ${cluster_name}."
