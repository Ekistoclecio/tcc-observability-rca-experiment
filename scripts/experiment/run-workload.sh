#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${repo_root}"
venv_python="${repo_root}/.venv-tcc-workload/bin/python"

if [[ -n "${TCC_ENV_FILE:-}" ]]; then
  if [[ ! -f "${TCC_ENV_FILE}" ]]; then
    echo "TCC_ENV_FILE does not exist: ${TCC_ENV_FILE}" >&2
    exit 2
  fi
  set -a
  # This is an explicitly supplied, trusted local configuration file.
  source "${TCC_ENV_FILE}"
  set +a
fi

if [[ -z "${TCC_RUN_ID:-}" ]]; then
  TCC_RUN_ID="workload-$(date -u '+%Y%m%dT%H%M%SZ')"
  export TCC_RUN_ID
fi
if [[ -z "${TCC_FRONTEND_URL:-}" ]]; then
  TCC_FRONTEND_URL="http://127.0.0.1:28080"
  export TCC_FRONTEND_URL
fi
TCC_ARTIFACT_DIR="${TCC_ARTIFACT_DIR:-${repo_root}/artifacts/workload/runs}"
export TCC_ARTIFACT_DIR

command -v kubectl >/dev/null || { echo 'kubectl is required for preflight checks' >&2; exit 2; }
command -v curl >/dev/null || { echo 'curl is required for the frontend health check' >&2; exit 2; }
kubectl get nodes >/dev/null

check_deployment() {
  local namespace="$1" name="$2" ready desired
  local deployment_state
  deployment_state="$(kubectl get deployment "${name}" -n "${namespace}" \
    -o jsonpath='{.status.readyReplicas} {.spec.replicas}' 2>/dev/null || true)"
  read -r ready desired <<< "${deployment_state}"
  ready="${ready:-0}"
  desired="${desired:-0}"
  if [[ "${desired}" == "0" || "${ready}" != "${desired}" ]]; then
    echo "preflight failed: ${namespace}/${name} ready=${ready} desired=${desired}" >&2
    exit 3
  fi
}

for deployment in frontend checkoutservice cartservice productcatalogservice \
  currencyservice paymentservice shippingservice emailservice recommendationservice \
  adservice redis-cart; do
  check_deployment default "${deployment}"
done
for deployment in prometheus-conventional loki-conventional jaeger-conventional; do
  check_deployment observability-conventional "${deployment}"
done
for deployment in prometheus-otel loki-otel jaeger-otel otel-collector; do
  check_deployment observability-otel "${deployment}"
done
original_loadgenerator_replicas="$(kubectl get deployment loadgenerator -n default -o jsonpath='{.spec.replicas}')"
if [[ "${original_loadgenerator_replicas}" != "0" ]]; then
  echo "preflight failed: scale the original loadgenerator to 0 before using the controlled runner" >&2
  exit 3
fi
ds_ready="$(kubectl get daemonset promtail-conventional -n observability-conventional \
  -o jsonpath='{.status.numberReady}/{.status.desiredNumberScheduled}')"
if [[ "${ds_ready%%/*}" != "${ds_ready##*/}" || "${ds_ready%%/*}" == "0" ]]; then
  echo "preflight failed: promtail-conventional Ready=${ds_ready}" >&2
  exit 3
fi
if [[ ! -x "${venv_python}" ]]; then
  "${repo_root}/scripts/experiment/setup-workload-venv.sh"
fi
if ! "${venv_python}" -c 'import locust' >/dev/null 2>&1; then
  echo 'Locust is not installed in the workload virtualenv; run setup-workload-venv.sh' >&2
  exit 2
fi

port_forward_pid=""
port_forward_log=""
runner_output="$(mktemp /tmp/tcc-workload-runner.XXXXXX)"
cleanup() {
  if [[ -n "${port_forward_pid}" ]]; then
    kill "${port_forward_pid}" 2>/dev/null || true
    wait "${port_forward_pid}" 2>/dev/null || true
  fi
  [[ -z "${port_forward_log}" ]] || rm -f "${port_forward_log}"
  rm -f "${runner_output}"
}
trap cleanup EXIT

if [[ "${TCC_FRONTEND_URL}" == "http://127.0.0.1:28080" && "${TCC_USE_PORT_FORWARD:-1}" == "1" ]]; then
  port_forward_log="$(mktemp /tmp/tcc-workload-port-forward.XXXXXX)"
  kubectl port-forward service/frontend 28080:80 >"${port_forward_log}" 2>&1 &
  port_forward_pid="$!"
  ready=0
  for attempt in $(seq 1 30); do
    if curl --fail --silent --output /dev/null "${TCC_FRONTEND_URL}/"; then ready=1; break; fi
    sleep 1
  done
  if [[ "${ready}" != "1" ]]; then
    cat "${port_forward_log}" >&2
    echo 'preflight failed: frontend port-forward did not become healthy' >&2
    exit 3
  fi
else
  curl --fail --silent --show-error --output /dev/null "${TCC_FRONTEND_URL}/"
fi

snapshot_pods() {
  kubectl get pods -A -o json | "${venv_python}" -c '
import json, sys
pods=json.load(sys.stdin)["items"]
print(json.dumps([{
  "namespace": p["metadata"]["namespace"],
  "name": p["metadata"]["name"],
  "phase": p.get("status", {}).get("phase"),
  "ready": all(c.get("ready", False) for c in p.get("status", {}).get("containerStatuses", [])),
  "restarts": sum(c.get("restartCount", 0) for c in p.get("status", {}).get("containerStatuses", []))
} for p in pods], separators=(",", ":")))'
}

before="$(snapshot_pods)"
export TCC_PODS_BEFORE_JSON="${before}"
set +e
"${venv_python}" -m src.loadgenerator.experiment.runner >"${runner_output}"
run_status="$?"
set -e
after="$(snapshot_pods)"

metadata_path="${TCC_ARTIFACT_DIR}/${TCC_RUN_ID}.json"
if [[ -f "${metadata_path}" ]]; then
  TCC_PODS_AFTER_JSON="${after}" "${venv_python}" - "${metadata_path}" <<'PY'
import json, os, pathlib, sys, tempfile
path=pathlib.Path(sys.argv[1])
data=json.loads(path.read_text())
data["pod_state_after"]=json.loads(os.environ["TCC_PODS_AFTER_JSON"])
with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as f:
    json.dump(data, f, indent=2, sort_keys=True)
    f.write("\n")
    temp=f.name
os.replace(temp, path)
PY
fi

if [[ -f "${metadata_path}" ]]; then
  cat "${metadata_path}"
fi
echo "workload metadata: ${metadata_path}"
exit "${run_status}"
