from __future__ import annotations

import json
import re

from .kube import deployment_status, kubectl_json, run_kubectl


SUT_DEPLOYMENTS = (
    "frontend", "checkoutservice", "cartservice", "productcatalogservice", "currencyservice",
    "paymentservice", "shippingservice", "emailservice", "recommendationservice", "adservice", "redis-cart",
)


def preflight(fault_class: str, allow_unsupported_chaos_mesh: bool) -> dict:
    run_kubectl("get", "nodes")
    nodes = kubectl_json("get", "nodes")
    version = nodes.get("items", [{}])[0].get("status", {}).get("nodeInfo", {}).get("kubeletVersion", "unknown")
    match = re.search(r"v\d+\.(\d+)", version)
    kubernetes_minor = int(match.group(1)) if match else None
    deployments = {name: deployment_status(name) for name in SUT_DEPLOYMENTS}
    not_ready = [name for name, status in deployments.items() if not status["ready"]]
    if not_ready:
        raise RuntimeError(f"SUT deployments not Ready: {not_ready}")
    active_chaos = run_kubectl("get", "stresschaos,networkchaos", "-A", "-o", "json", check=False)
    if active_chaos.returncode == 0:
        items = json.loads(active_chaos.stdout).get("items", [])
        unrecovered = [item.get("metadata", {}).get("name") for item in items if not any(
            condition.get("type") == "AllRecovered" and str(condition.get("status", "")).lower() == "true"
            for condition in item.get("status", {}).get("conditions", [])
        )]
        if unrecovered:
            raise RuntimeError(f"active or unrecovered Chaos Mesh resources exist: {unrecovered}")
    workload_replicas = int(kubectl_json("get", "deployment", "loadgenerator", "-n", "default").get("spec", {}).get("replicas", 0))
    if workload_replicas != 0:
        raise RuntimeError("original Online Boutique loadgenerator must remain scaled to zero")
    backend_deployments = (
        ("observability-conventional", "prometheus-conventional"),
        ("observability-conventional", "loki-conventional"),
        ("observability-conventional", "jaeger-conventional"),
        ("observability-otel", "prometheus-otel"),
        ("observability-otel", "loki-otel"),
        ("observability-otel", "jaeger-otel"),
        ("observability-otel", "otel-collector"),
    )
    backend_status = {}
    for namespace, name in backend_deployments:
        status = deployment_status(name, namespace)
        if not status["ready"]:
            raise RuntimeError(f"observability backend is not Ready: {namespace}/{name}")
        backend_status[f"{namespace}/{name}"] = status
    promtail = kubectl_json("get", "daemonset", "promtail-conventional", "-n", "observability-conventional").get("status", {})
    if int(promtail.get("desiredNumberScheduled", 0)) == 0 or int(promtail.get("numberReady", 0)) != int(promtail.get("desiredNumberScheduled", 0)):
        raise RuntimeError("promtail-conventional is not Ready on all nodes")
    version_supported = kubernetes_minor in (30, 31, 32, 33, 34, 35)
    chaos = None
    if fault_class in ("F1", "F2"):
        if not version_supported and not allow_unsupported_chaos_mesh:
            raise RuntimeError(
                f"Kubernetes {version} is outside Chaos Mesh 2.8.6 tested minors 30..35; "
                "repeat with --allow-unsupported-chaos-mesh only for a documented smoke test"
            )
        controller = kubectl_json("get", "deployment", "chaos-controller-manager", "-n", "chaos-mesh")
        daemon = kubectl_json("get", "daemonset", "chaos-daemon", "-n", "chaos-mesh")
        chaos = {
            "version": controller.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [{}])[0].get("image", "unknown").rsplit(":", 1)[-1],
            "controller_ready": int(controller.get("status", {}).get("readyReplicas", 0)) > 0,
            "daemon_ready": int(daemon.get("status", {}).get("numberReady", 0)) == int(daemon.get("status", {}).get("desiredNumberScheduled", -1)),
            "kubernetes_version": version,
            "officially_tested_kubernetes_minor": version_supported,
        }
        if not chaos["controller_ready"] or not chaos["daemon_ready"]:
            raise RuntimeError("Chaos Mesh components are not Ready")
    return {
        "kubernetes_version": version,
        "kubernetes_minor_supported_by_chaos_mesh_2_8_6": version_supported,
        "sut_deployments": deployments,
        "observability_backends": backend_status,
        "original_loadgenerator_replicas": workload_replicas,
        "chaos_mesh": chaos,
    }
