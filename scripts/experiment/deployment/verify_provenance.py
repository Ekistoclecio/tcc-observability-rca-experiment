#!/usr/bin/env python3
"""Verify Git revision, local image, Kind image, Deployment, and running Pod."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
SERVICES = (
    "frontend",
    "checkoutservice",
    "cartservice",
    "productcatalogservice",
    "currencyservice",
    "paymentservice",
    "shippingservice",
    "emailservice",
    "recommendationservice",
    "adservice",
)
REVISION_LABEL = "org.opencontainers.image.revision"
VERSIONS_FILE = ROOT / "config/experiment/environment/versions.env"
IMAGE_CONFIG_READER = ROOT / "scripts/experiment/deployment/image_config_digest.py"


def run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, text=True, capture_output=True, check=False)
    if check and result.returncode:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(args)}\n"
            f"{result.stderr.strip()}"
        )
    return result


def docker_config_identity(docker: str, reference: str) -> dict[str, str]:
    archive = subprocess.Popen(
        [docker, "image", "save", reference], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert archive.stdout is not None
    parsed = subprocess.run(
        [sys.executable, str(IMAGE_CONFIG_READER)],
        stdin=archive.stdout,
        capture_output=True,
        text=True,
        check=False,
    )
    archive.stdout.close()
    docker_stderr = archive.communicate()[1].decode(errors="replace")
    if archive.returncode or parsed.returncode:
        raise RuntimeError(
            f"could not read Docker image config for {reference}: "
            f"{docker_stderr.strip()} {parsed.stderr.strip()}"
        )
    return json.loads(parsed.stdout)


def digest(value: str) -> str:
    match = re.search(r"sha256:[0-9a-f]{64}$", value)
    if not match:
        raise ValueError(f"unsupported image digest: {value}")
    return match.group(0)


def working_tree_is_clean(status_porcelain: str) -> bool:
    return not status_porcelain.strip()


def load_versions() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in VERSIONS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    return values


def same_digest(left: str | None, right: str | None) -> bool:
    try:
        return digest(left or "") == digest(right or "")
    except ValueError:
        return False


def validate_service(
    service: str,
    expected_revision: str,
    expected_reference: str,
    local_image: dict[str, Any],
    pod: dict[str, Any],
    runtime_image: dict[str, Any],
) -> dict[str, Any]:
    problems: list[str] = []
    local_config_id = local_image.get("Id")
    image_config_id = local_image.get("ExperimentConfigDigest")
    image_manifest_id = local_image.get("ExperimentIndexManifestDigest")
    local_labels = (local_image.get("Config") or {}).get("Labels") or {}
    runtime_status = runtime_image.get("status") or {}
    runtime_info = runtime_image.get("info") or {}
    image_spec = runtime_info.get("imageSpec") or {}
    runtime_config = image_spec.get("config") or {}
    runtime_labels = runtime_config.get("Labels") or {}
    runtime_id = runtime_status.get("id")
    pod_spec = pod.get("spec") or {}
    pod_status = pod.get("status") or {}

    if expected_reference not in (local_image.get("RepoTags") or []):
        problems.append("local reference mismatch")
    if local_labels.get(REVISION_LABEL) != expected_revision:
        problems.append("local OCI revision missing or mismatched")
    if runtime_labels.get(REVISION_LABEL) != expected_revision:
        problems.append("Kind runtime OCI revision missing or mismatched")
    if not same_digest(image_config_id, runtime_id):
        problems.append("local image config digest differs from Kind CRI config ID")
    repo_tags = runtime_status.get("repoTags") or []
    expected_node_reference = f"docker.io/{expected_reference}"
    if expected_node_reference not in repo_tags:
        problems.append("Kind CRI image reference is missing or mismatched")

    containers = pod_spec.get("containers") or []
    if len(containers) != 1:
        problems.append(f"expected one application container, found {len(containers)}")
        container_name = None
    else:
        container_name = containers[0].get("name")
        if containers[0].get("image") != expected_reference:
            problems.append("Pod spec image reference mismatch")
        if containers[0].get("imagePullPolicy") != "IfNotPresent":
            problems.append("Pod imagePullPolicy is not IfNotPresent")

    statuses = pod_status.get("containerStatuses") or []
    status = next(
        (item for item in statuses if item.get("name") == container_name), None
    )
    if status is None:
        problems.append("Pod has no status for the application container")
        running_id = None
    else:
        running_id = status.get("imageID")
        if not status.get("ready"):
            problems.append("application container is not Ready")
        if not same_digest(running_id, image_config_id):
            problems.append("Pod imageID differs from the local image config digest")

    return {
        "service": service,
        "expected_git_revision": expected_revision,
        "image_reference": expected_reference,
        "local_docker_image_id": local_config_id,
        "local_index_manifest_digest": image_manifest_id,
        "local_image_config_digest": image_config_id,
        "image_revision": local_labels.get(REVISION_LABEL),
        "kind_image_id": runtime_id,
        "kind_image_revision": runtime_labels.get(REVISION_LABEL),
        "running_pod": pod.get("metadata", {}).get("name"),
        "running_image_reference": (
            containers[0].get("image") if len(containers) == 1 else None
        ),
        "running_image_id": running_id,
        "comparison_method": "local Docker config ID == Kind CRI config ID == Pod imageID; OCI revision label read from local Docker and Kind CRI image config",
        "status": "PASS" if not problems else "FAIL",
        "problems": problems,
    }


def validate_all(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_service = {record.get("service"): record for record in records}
    missing = sorted(set(SERVICES) - set(by_service))
    unexpected = sorted(set(by_service) - set(SERVICES))
    return {
        "services": records,
        "expected_service_count": len(SERVICES),
        "validated_service_count": len(set(by_service)),
        "validated_pod_count": len(records),
        "missing_services": missing,
        "unexpected_services": unexpected,
        "status": (
            "PASS"
            if not missing
            and not unexpected
            and all(record.get("status") == "PASS" for record in records)
            else "FAIL"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    kubectl = os.environ.get("KUBECTL_BIN", "kubectl")
    docker = os.environ.get("DOCKER_BIN", "docker")
    kind = os.environ.get("KIND_BIN", "kind")
    versions = load_versions()
    namespace = versions["SUT_NAMESPACE"]
    cluster = versions["KIND_CLUSTER_NAME"]

    try:
        dirty = run(["git", "-C", str(ROOT), "status", "--porcelain"]).stdout
        if not working_tree_is_clean(dirty):
            raise RuntimeError("working tree is dirty; provenance preflight is blocked")
        revision = run(["git", "-C", str(ROOT), "rev-parse", "HEAD"]).stdout.strip()
        listed = run([kind, "get", "clusters"]).stdout.split()
        if cluster not in listed:
            raise RuntimeError(f"experiment Kind cluster {cluster} is absent")
        node_names = run(
            [
                docker,
                "ps",
                "--filter",
                f"label=io.x-k8s.kind.cluster={cluster}",
                "--format",
                "{{.Names}}",
            ]
        ).stdout.split()
        if len(node_names) != 1:
            raise RuntimeError(f"expected one running Kind node, found {node_names}")
        node = node_names[0]
        reference_base = f"{versions['EXPERIMENT_IMAGE_PREFIX']}/"

        records: list[dict[str, Any]] = []
        for service in SERVICES:
            reference = f"{reference_base}{service}:{revision}"
            local = json.loads(
                run([docker, "image", "inspect", reference]).stdout
            )[0]
            image_identity = docker_config_identity(docker, reference)
            local["ExperimentConfigDigest"] = image_identity["config_digest"]
            local["ExperimentIndexManifestDigest"] = image_identity[
                "index_manifest_digest"
            ]
            deployment = json.loads(
                run(
                    [
                        kubectl,
                        "--context",
                        f"kind-{cluster}",
                        "-n",
                        namespace,
                        "get",
                        "deployment",
                        service,
                        "-o",
                        "json",
                    ]
                ).stdout
            )
            desired = deployment.get("spec", {}).get("replicas", 1)
            ready = deployment.get("status", {}).get("readyReplicas", 0)
            if desired < 1 or ready != desired:
                raise RuntimeError(
                    f"{service} Deployment not Ready: desired={desired}, ready={ready}"
                )
            pods = json.loads(
                run(
                    [
                        kubectl,
                        "--context",
                        f"kind-{cluster}",
                        "-n",
                        namespace,
                        "get",
                        "pods",
                        "-l",
                        f"app={service}",
                        "-o",
                        "json",
                    ]
                ).stdout
            ).get("items", [])
            if len(pods) != desired:
                raise RuntimeError(
                    f"{service} Pod count mismatch: desired={desired}, pods={len(pods)}"
                )
            for pod in pods:
                containers = pod.get("spec", {}).get("containers", [])
                if len(containers) != 1 or containers[0].get("image") != reference:
                    runtime = {"status": {}, "info": {}}
                else:
                    inspected = run(
                        [docker, "exec", node, "crictl", "inspecti", reference]
                    )
                    runtime = json.loads(inspected.stdout)
                record = validate_service(
                    service, revision, reference, local, pod, runtime
                )
                records.append(record)

        report = validate_all(records)
        report["expected_git_revision"] = revision
        report["branch"] = run(
            ["git", "-C", str(ROOT), "branch", "--show-current"]
        ).stdout.strip()
        report["working_tree"] = "CLEAN"
    except Exception as exc:
        report = {
            "status": "FAIL",
            "error": str(exc),
            "working_tree": "UNKNOWN",
        }

    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    sys.stdout.write(payload)
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
