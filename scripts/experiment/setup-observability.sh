#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

kubectl apply -k "${repo_root}/deployments/experiment"
# ConfigMaps use stable names in this overlay, so applying a Collector change
# does not itself recreate the pod.
kubectl rollout restart deployment/otel-collector -n observability-otel
kubectl rollout status deployment/frontend --timeout=240s
kubectl rollout status deployment/checkoutservice --timeout=240s
kubectl rollout status deployment/cartservice --timeout=240s
kubectl rollout status deployment/productcatalogservice --timeout=240s
kubectl rollout status deployment/currencyservice --timeout=240s
kubectl rollout status deployment/paymentservice --timeout=240s
kubectl rollout status deployment/shippingservice --timeout=240s
kubectl rollout status deployment/emailservice --timeout=240s
kubectl rollout status deployment/recommendationservice --timeout=240s
kubectl rollout status deployment/adservice --timeout=240s
kubectl rollout status deployment/redis-cart --timeout=240s
kubectl rollout status deployment/loadgenerator --timeout=240s
kubectl rollout status deployment/prometheus-conventional -n observability-conventional --timeout=240s
kubectl rollout status deployment/loki-conventional -n observability-conventional --timeout=240s
kubectl rollout status deployment/jaeger-conventional -n observability-conventional --timeout=240s
kubectl rollout status deployment/otel-collector -n observability-otel --timeout=240s
kubectl rollout status deployment/prometheus-otel -n observability-otel --timeout=240s
kubectl rollout status deployment/loki-otel -n observability-otel --timeout=240s
kubectl rollout status deployment/jaeger-otel -n observability-otel --timeout=240s

kubectl get pods
kubectl get pods -n observability-conventional
kubectl get pods -n observability-otel
