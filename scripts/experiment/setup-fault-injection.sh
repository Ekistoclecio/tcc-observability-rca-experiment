#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${repo_root}"
command -v kubectl >/dev/null || { echo 'kubectl is required' >&2; exit 2; }
kubectl get nodes >/dev/null

# A validation image set must have one source revision across all SUT services.
TCC_LOAD_IMAGES=1 "${repo_root}/scripts/experiment/build-application-images.sh"
kubectl apply -k deployments/experiment
"${repo_root}/scripts/experiment/deploy-application-images.sh"
kubectl rollout status deployment/paymentservice -n default --timeout=180s
kubectl exec deployment/paymentservice -n default -c server -- sh -c \
  'test "${TCC_FAULT_INJECTION_ENABLED}" = true && test -d /tmp/tcc-faults && test -w /tmp/tcc-faults'
echo 'paymentservice F4 hook is enabled and its emptyDir control path is writable.'
