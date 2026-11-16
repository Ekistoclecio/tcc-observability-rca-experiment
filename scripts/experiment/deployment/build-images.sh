#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../../.." && pwd)"
# shellcheck source=../../../config/experiment/environment/versions.env
source "$REPO_ROOT/config/experiment/environment/versions.env"

if [[ -n "$(git -C "$REPO_ROOT" status --porcelain)" ]]; then
  printf 'ERROR: imagens experimentais exigem working tree limpa.\n' >&2
  exit 1
fi

revision="$(git -C "$REPO_ROOT" rev-parse HEAD)"
[[ "$revision" =~ ^[0-9a-f]{40}$ ]] || {
  printf 'ERROR: revisão Git inválida: %s\n' "$revision" >&2
  exit 1
}

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

for service in "${services[@]}"; do
  context="$REPO_ROOT/src/$service"
  dockerfile="$context/Dockerfile"
  if [[ "$service" == "cartservice" ]]; then
    context="$REPO_ROOT/src/cartservice/src"
    dockerfile="$context/Dockerfile"
  fi
  [[ -f "$dockerfile" ]] || {
    printf 'ERROR: Dockerfile ausente para %s: %s\n' "$service" "$dockerfile" >&2
    exit 1
  }

  image="$EXPERIMENT_IMAGE_PREFIX/$service:$revision"
  printf 'Building %s from %s\n' "$image" "${context#"$REPO_ROOT"/}"
  docker build \
    --platform linux/amd64 \
    --label "org.opencontainers.image.revision=$revision" \
    --label "org.opencontainers.image.source=https://github.com/GoogleCloudPlatform/microservices-demo" \
    --tag "$image" \
    --file "$dockerfile" \
    "$context"
done
