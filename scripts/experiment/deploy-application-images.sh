#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cluster_name="${KIND_CLUSTER_NAME:-tcc-baseline}"
services=(
  frontend checkoutservice cartservice productcatalogservice currencyservice
  paymentservice shippingservice emailservice recommendationservice adservice
)

git_revision="$(git -C "${repo_root}" rev-parse HEAD)"
if [[ -n "$(git -C "${repo_root}" status --porcelain)" ]]; then
  echo "Refusing deployment from a dirty working tree." >&2
  exit 2
fi
current_context="$(kubectl config current-context)"
if [[ "${current_context}" != "kind-${cluster_name}" ]]; then
  echo "Refusing deployment: current context is ${current_context}, expected kind-${cluster_name}." >&2
  exit 5
fi

for service in "${services[@]}"; do
  image="${service}:tcc-${git_revision}"
  image_revision="$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "${image}")"
  if [[ "${image_revision}" != "${git_revision}" ]]; then
    echo "Local image ${image} has revision ${image_revision:-<missing>}; expected ${git_revision}." >&2
    exit 3
  fi
done

if ! docker ps --format '{{.Names}}' | grep -Fxq "${cluster_name}-control-plane"; then
  echo "Kind control-plane container ${cluster_name}-control-plane is not running." >&2
  exit 4
fi

for service in "${services[@]}"; do
  image="${service}:tcc-${git_revision}"
  kubectl set image "deployment/${service}" "server=${image}" -n default
done

for service in "${services[@]}"; do
  kubectl rollout status "deployment/${service}" -n default --timeout=240s
done

python3 "${repo_root}/scripts/experiment/validate_deployment_provenance.py" \
  --cluster-name "${cluster_name}"
