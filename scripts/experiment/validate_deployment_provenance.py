#!/usr/bin/env python3
"""Hard preflight proving Git revision -> local image -> running SUT Pods."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
OCI_REVISION = "org.opencontainers.image.revision"
SERVICES = (
    "frontend", "checkoutservice", "cartservice", "productcatalogservice",
    "currencyservice", "paymentservice", "shippingservice", "emailservice",
    "recommendationservice", "adservice",
)


def normalize_config_digest(value: str | None) -> str | None:
    """Accept only image config IDs, never repository or OCI index digests."""
    if not value:
        return None
    match = re.fullmatch(
        r"(?:(?:containerd|docker|cri-o)://)?(sha256:[0-9a-f]{64})", value.lower()
    )
    return match.group(1) if match else None


def evaluate_service(
    service: str,
    expected_revision: str,
    image_reference: str,
    local_image_id: str | None,
    image_revision: str | None,
    deployment_image: str | None,
    desired_replicas: int,
    ready_replicas: int,
    pods: list[dict[str, Any]],
) -> dict[str, Any]:
    failures: list[str] = []
    if not local_image_id:
        failures.append("local image ID unavailable")
    if image_revision != expected_revision:
        failures.append(f"image OCI revision {image_revision or '<missing>'} != expected {expected_revision}")
    if deployment_image != image_reference:
        failures.append(f"Deployment image {deployment_image or '<missing>'} != expected image {image_reference}")
    if desired_replicas < 1 or ready_replicas != desired_replicas:
        failures.append(f"Deployment readiness {ready_replicas}/{desired_replicas}")
    if len(pods) != desired_replicas:
        failures.append(f"selected Pod count {len(pods)} != desired replicas {desired_replicas}")

    checked_pods = []
    for pod in pods:
        running_digest = normalize_config_digest(pod.get("image_id"))
        local_digest = normalize_config_digest(local_image_id)
        pod_ok = True
        if not pod.get("ready"):
            failures.append(f"Pod {pod.get('name')} is not Ready")
            pod_ok = False
        if pod.get("image") != image_reference:
            failures.append(f"Pod {pod.get('name')} image reference does not match expected image")
            pod_ok = False
        if not running_digest or not local_digest or running_digest != local_digest:
            failures.append(f"Pod {pod.get('name')} runtime imageID does not match local image config digest")
            pod_ok = False
        checked_pods.append({
            "pod": pod.get("name"),
            "container": pod.get("container"),
            "image_reference": pod.get("image"),
            "running_image_id": pod.get("image_id"),
            "running_config_digest": running_digest,
            "ready": bool(pod.get("ready")),
            "passed": pod_ok,
        })

    return {
        "service": service,
        "namespace": "default",
        "deployment": service,
        "expected_git_revision": expected_revision,
        "local_image_reference": image_reference,
        "local_image_id": local_image_id,
        "image_revision": image_revision,
        "deployment_image_reference": deployment_image,
        "desired_replicas": desired_replicas,
        "ready_replicas": ready_replicas,
        "running_pod": checked_pods[0]["pod"] if len(checked_pods) == 1 else None,
        "running_image_reference": checked_pods[0]["image_reference"] if len(checked_pods) == 1 else None,
        "running_pods": checked_pods,
        "comparison_method": "OCI revision label plus exact image reference and local config digest == Pod runtime config digest",
        "failures": failures,
        "status": "PASS" if not failures else "FAIL",
    }


def validate_records(records: list[dict[str, Any]], working_tree_clean: bool) -> dict[str, Any]:
    failures = [] if working_tree_clean else ["working tree is dirty"]
    for record in records:
        if record.get("status") != "PASS":
            failures.append(f"{record.get('service', '<unknown>')}: service provenance failed")
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "working_tree_clean": working_tree_clean,
        "services_expected": len(SERVICES),
        "services_validated": len(records),
        "passed": len(records) == len(SERVICES) and working_tree_clean and not failures,
        "build_provenance_passed": len(records) == len(SERVICES) and all(
            record.get("image_revision") == record.get("expected_git_revision") for record in records
        ),
        "kind_image_loading_passed": len(records) == len(SERVICES) and all(
            bool(record.get("running_pods")) and all(pod.get("passed") for pod in record["running_pods"])
            for record in records
        ),
        "pod_rollout_passed": len(records) == len(SERVICES) and all(
            record.get("deployment_image_reference") == record.get("local_image_reference")
            and record.get("ready_replicas") == record.get("desired_replicas")
            and record.get("desired_replicas", 0) > 0 for record in records
        ),
        "revision_mismatches": sum(
            record.get("image_revision") != record.get("expected_git_revision") for record in records
        ),
        "missing_revision_metadata": sum(not record.get("image_revision") for record in records),
        "unproven_running_images": sum(
            not record.get("running_pods") or any(not pod.get("passed") for pod in record["running_pods"])
            for record in records
        ),
        "failures": failures,
        "services": records,
    }


def run_json(command: list[str]) -> dict[str, Any]:
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def run_text(command: list[str]) -> str:
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def git_state() -> tuple[str, bool]:
    revision = run_text(["git", "-C", str(REPO), "rev-parse", "HEAD"])
    status = run_text(["git", "-C", str(REPO), "status", "--porcelain"])
    return revision, not bool(status)


def collect_service(service: str, revision: str) -> dict[str, Any]:
    image = f"{service}:tcc-{revision}"
    local_image = run_json(["docker", "image", "inspect", image])[0]
    local_image_id = local_image.get("Id")
    image_revision = (local_image.get("Config", {}).get("Labels") or {}).get(OCI_REVISION)

    deployment = run_json(["kubectl", "get", "deployment", service, "-n", "default", "-o", "json"])
    containers = deployment.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    app_container = next((container for container in containers if container.get("name") == "server"), None)
    deployment_image = app_container.get("image") if app_container else None
    selector = deployment.get("spec", {}).get("selector", {}).get("matchLabels", {})
    if not selector:
        pods_data = {"items": []}
    else:
        selector_arg = ",".join(f"{key}={value}" for key, value in sorted(selector.items()))
        pods_data = run_json(["kubectl", "get", "pods", "-n", "default", "-l", selector_arg, "-o", "json"])

    pods = []
    for pod in pods_data.get("items", []):
        statuses = pod.get("status", {}).get("containerStatuses", [])
        container_status = next((item for item in statuses if item.get("name") == "server"), {})
        pod_spec = next((item for item in pod.get("spec", {}).get("containers", []) if item.get("name") == "server"), {})
        pods.append({
            "name": pod.get("metadata", {}).get("name"),
            "container": "server",
            "image": pod_spec.get("image"),
            "image_id": container_status.get("imageID"),
            "ready": bool(container_status.get("ready")) and pod.get("status", {}).get("phase") == "Running",
        })

    return evaluate_service(
        service=service,
        expected_revision=revision,
        image_reference=image,
        local_image_id=local_image_id,
        image_revision=image_revision,
        deployment_image=deployment_image,
        desired_replicas=int(deployment.get("spec", {}).get("replicas", 0)),
        ready_replicas=int(deployment.get("status", {}).get("readyReplicas", 0)),
        pods=pods,
    )


def write_artifacts(report: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "provenance.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Deployment provenance report", "",
        f"Expected Git revision: {report.get('expected_git_revision', 'unknown')}",
        f"Working tree clean: {report['working_tree_clean']}",
        f"Overall: {'PASS' if report['passed'] else 'FAIL'}", "",
        "## Build, Kind and Pod chain", "",
        f"Build revision label verified: {report['build_provenance_passed']}",
        f"Kind-loaded image verified through running Pod config digest: {report['kind_image_loading_passed']}",
        f"Deployment rollout/image reference verified: {report['pod_rollout_passed']}", "",
        "The build script rejects a dirty worktree, tags each image with the full Git SHA, and writes the OCI revision label. The deploy helper checks the Kind context, updates all ten Deployments to immutable revision tags, waits for rollout, then invokes this validator.", "",
        "Digest comparison is performed only between the local image config digest and the Pod runtime config digest.",
        "The OCI/index digest, repository digest, tag, and config digest are distinct identifiers. Values that are not explicitly config IDs are rejected rather than compared.", "",
        "| Service | Image revision | Pod imageID | Status |", "|---|---|---|---|",
    ]
    for service in report["services"]:
        pod_ids = ", ".join(pod.get("running_image_id") or "missing" for pod in service["running_pods"])
        lines.append(f"| {service['service']} | {service.get('image_revision') or 'missing'} | {pod_ids} | {service['status']} |")
        for failure in service["failures"]:
            lines.append(f"  - {failure}")
    lines += ["", "Failures:"] + ([f"- {item}" for item in report["failures"]] or ["- None"])
    (output_dir / "provenance-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    readme = """# Deployment provenance

This directory records the Git revision embedded in each locally built SUT image and verifies that the Deployment and ready Pod use that exact immutable revision tag. The Pod runtime config digest must equal the local Docker image config digest, and the image must carry the matching OCI revision label.

The report deliberately distinguishes image/index digest, repository digest, config image ID, runtime imageID, and mutable/immutable tags. No fault or workload is started by this validator.
"""
    (output_dir / "README.md").write_text(readme, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster-name", default="tcc-baseline")
    parser.add_argument("--output-dir", type=Path, default=REPO / "audit/deployment-provenance")
    args = parser.parse_args()
    revision, clean = git_state()
    current_context = None
    context_error = None
    try:
        current_context = run_text(["kubectl", "config", "current-context"])
    except subprocess.CalledProcessError as exc:
        context_error = str(exc)
    image_records = []
    for service in SERVICES:
        try:
            image_records.append(collect_service(service, revision))
        except (subprocess.CalledProcessError, json.JSONDecodeError, OSError) as exc:
            image_records.append(evaluate_service(
                service, revision, f"{service}:tcc-{revision}", None, None, None, 0, 0, []
            ) | {"collection_error": str(exc)})
    report = validate_records(image_records, clean)
    report["expected_git_revision"] = revision
    report["cluster_name"] = args.cluster_name
    report["kubectl_context"] = current_context
    if context_error:
        report["failures"].append(f"could not read kubectl context: {context_error}")
    elif current_context != f"kind-{args.cluster_name}":
        report["failures"].append(f"kubectl context {current_context!r} != kind-{args.cluster_name}")
    report["passed"] = report["passed"] and not report["failures"]
    report["oci_revision_label"] = OCI_REVISION
    report["kind_loading_proven_by"] = "running Pod runtime config digest equals inspected local image config digest"
    report["deployment_rollout_proven_by"] = "Deployment references immutable revision tag and all desired replicas are Ready"
    write_artifacts(report, args.output_dir)
    print(json.dumps({"passed": report["passed"], "services": report["services_validated"], "report": str(args.output_dir)}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
