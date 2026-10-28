#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${repo_root}"
command -v kubectl >/dev/null || { echo 'kubectl is required' >&2; exit 2; }
kubectl get nodes >/dev/null
kubectl scale deployment/loadgenerator --replicas=0 -n default >/dev/null

# The build is deliberately sequential and follows the resource caps used by Phase 2.
TCC_LOAD_IMAGES=1 "${repo_root}/scripts/experiment/build-application-images.sh" paymentservice
kubectl apply -k deployments/experiment
kubectl rollout status deployment/paymentservice -n default --timeout=180s
kubectl exec deployment/paymentservice -n default -c server -- sh -c \
  'test "${TCC_FAULT_INJECTION_ENABLED}" = true && test -d /tmp/tcc-faults && test -w /tmp/tcc-faults'
echo 'paymentservice F4 hook is enabled and its emptyDir control path is writable.'
