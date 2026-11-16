#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

require_tools
require_cluster
"$KUBECTL_BIN" --context "$KIND_CONTEXT" wait \
  --for=condition=Available deployment --all \
  --namespace "$SUT_NAMESPACE" --timeout=5m

deployments_json="$("$KUBECTL_BIN" --context "$KIND_CONTEXT" \
  get deployments --namespace "$SUT_NAMESPACE" -o json)"
EXPECTED_DEPLOYMENTS="${sut_deployments[*]}" python3 -c '
import json, os, sys
data=json.loads(sys.stdin.read())
expected=set(os.environ["EXPECTED_DEPLOYMENTS"].split())
actual={x["metadata"]["name"] for x in data.get("items", [])}
if actual != expected:
    print(f"deployment set mismatch: expected={sorted(expected)} actual={sorted(actual)}", file=sys.stderr)
    raise SystemExit(1)
for item in data["items"]:
    name=item["metadata"]["name"]
    desired=item.get("spec", {}).get("replicas", 1)
    ready=item.get("status", {}).get("readyReplicas", 0)
    print(f"{name}: {ready}/{desired} Ready")
    if ready != desired or desired < 1:
        raise SystemExit(f"deployment not fully Ready: {name}")
' <<<"$deployments_json"

local_port="${TCC_FRONTEND_LOCAL_PORT:-18080}"
portforward_log="$(mktemp)"
"$KUBECTL_BIN" --context "$KIND_CONTEXT" --namespace "$SUT_NAMESPACE" \
  port-forward "service/frontend" "${local_port}:80" >"$portforward_log" 2>&1 &
portforward_pid=$!
cleanup_portforward() {
  kill "$portforward_pid" >/dev/null 2>&1 || true
  wait "$portforward_pid" >/dev/null 2>&1 || true
  rm -f "$portforward_log"
}
trap cleanup_portforward EXIT

http_code=""
for _ in $(seq 1 30); do
  if curl --silent --fail --output /dev/null --max-time 2 \
      "http://127.0.0.1:${local_port}/_healthz" 2>/dev/null; then
    break
  fi
  sleep 1
done
curl --silent --fail --output /dev/null --max-time 5 \
  "http://127.0.0.1:${local_port}/_healthz" || {
  cat "$portforward_log" >&2
  fail "frontend readiness endpoint não respondeu HTTP 200 via port-forward"
}

http_code="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
  --max-time 10 "http://127.0.0.1:${local_port}/")"
[[ "$http_code" == "200" ]] || \
  fail "request funcional ao frontend retornou HTTP ${http_code}, esperado HTTP 200"
printf 'Frontend request: HTTP %s\n' "$http_code"
