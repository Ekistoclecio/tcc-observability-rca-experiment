#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
tmp_dir="$(mktemp -d /tmp/tcc-observability-validation.XXXXXX)"
port_forward_pids=()
previous_load_replicas="$(kubectl get deployment loadgenerator -o jsonpath='{.spec.replicas}' 2>/dev/null || true)"

cleanup() {
  for pid in "${port_forward_pids[@]}"; do
    kill "${pid}" 2>/dev/null || true
  done
  if [[ -n "${previous_load_replicas}" ]]; then
    kubectl scale deployment/loadgenerator --replicas="${previous_load_replicas}" >/dev/null || true
  fi
  rm -rf "${tmp_dir}"
}
trap cleanup EXIT

if [[ -n "${previous_load_replicas}" ]]; then
  kubectl scale deployment/loadgenerator --replicas=0 >/dev/null
fi

forward() {
  local namespace="$1"
  local service="$2"
  local ports="$3"
  kubectl --namespace "${namespace}" port-forward "service/${service}" "${ports}" \
    >"${tmp_dir}/${service}.log" 2>&1 &
  port_forward_pids+=("$!")
}

forward default frontend 28080:80
forward observability-conventional jaeger-conventional 26686:16686
forward observability-otel jaeger-otel 27686:16686
forward observability-conventional prometheus-conventional 29091:9090
forward observability-otel prometheus-otel 29092:9090
forward observability-conventional loki-conventional 23100:3100
forward observability-otel loki-otel 23101:3100

for endpoint in \
  http://127.0.0.1:28080/ \
  http://127.0.0.1:26686/api/services \
  http://127.0.0.1:27686/api/services \
  http://127.0.0.1:29091/-/ready \
  http://127.0.0.1:29092/-/ready \
  http://127.0.0.1:23100/ready \
  http://127.0.0.1:23101/ready; do
  ready=0
  for _attempt in $(seq 1 30); do
    if curl --fail --silent --output /dev/null "${endpoint}"; then
      ready=1
      break
    fi
    sleep 1
  done
  if (( ready == 0 )); then
    echo "Endpoint did not become ready: ${endpoint}" >&2
    exit 4
  fi
done

checkout_output="$(${script_dir}/deterministic-checkout.sh)"
printf '%s\n' "${checkout_output}"
checkout_start_us="$(sed -n 's/^checkout_start_us=//p' <<<"${checkout_output}")"
checkout_end_us="$(sed -n 's/^checkout_end_us=//p' <<<"${checkout_output}")"

CHECKOUT_START_US="${checkout_start_us}" \
CHECKOUT_END_US="${checkout_end_us}" \
  node "${script_dir}/validate-isolation.mjs"

kubectl get pods
kubectl get pods -n observability-conventional
kubectl get pods -n observability-otel
