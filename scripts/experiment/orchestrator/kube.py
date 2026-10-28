"""Small kubectl adapter used by fault injectors and preflight checks."""

from __future__ import annotations

import json
import subprocess
import time
from typing import Callable


class KubectlError(RuntimeError):
    pass


def run_kubectl(*args: str, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["kubectl", *args], input=input_text, capture_output=True, text=True, check=False,
    )
    if check and result.returncode:
        raise KubectlError(
            f"kubectl {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()}"
        )
    return result


def kubectl_json(*args: str) -> dict:
    result = run_kubectl(*args, "-o", "json")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise KubectlError(f"kubectl returned invalid JSON for {' '.join(args)}") from exc


def wait_until(predicate: Callable[[], tuple[bool, dict]], timeout: float, description: str, interval: float = 0.25) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        ready, evidence = predicate()
        last = evidence
        if ready:
            return evidence
        time.sleep(interval)
    raise TimeoutError(f"timed out waiting for {description}; last evidence={last}")


def deployment_status(name: str, namespace: str = "default") -> dict:
    deployment = kubectl_json("get", "deployment", name, "-n", namespace)
    spec = deployment.get("spec", {})
    status = deployment.get("status", {})
    desired = int(spec.get("replicas", 1))
    ready = int(status.get("readyReplicas", 0))
    return {
        "name": name,
        "namespace": namespace,
        "desired_replicas": desired,
        "ready_replicas": ready,
        "available_replicas": int(status.get("availableReplicas", 0)),
        "ready": desired > 0 and ready == desired,
        "uid": deployment.get("metadata", {}).get("uid"),
    }


def endpoint_count(service: str, namespace: str = "default") -> int:
    endpoints = kubectl_json("get", "endpoints", service, "-n", namespace)
    return sum(len(subset.get("addresses", [])) for subset in endpoints.get("subsets", []))


def ready_pods(selector: str, namespace: str = "default") -> list[dict]:
    result = run_kubectl("get", "pods", "-n", namespace, "-l", selector, "-o", "json")
    pods = json.loads(result.stdout).get("items", [])
    answer = []
    for pod in pods:
        statuses = pod.get("status", {}).get("containerStatuses", [])
        answer.append({
            "name": pod["metadata"]["name"],
            "uid": pod["metadata"]["uid"],
            "ready": bool(statuses) and all(status.get("ready", False) for status in statuses),
            "restarts": sum(int(status.get("restartCount", 0)) for status in statuses),
            "labels": pod.get("metadata", {}).get("labels", {}),
        })
    return answer
