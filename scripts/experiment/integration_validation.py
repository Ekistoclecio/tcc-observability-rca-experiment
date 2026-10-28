from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


REPO = Path(__file__).resolve().parents[2]
ARTIFACT_ROOT = REPO / "artifacts" / "integration-validation"
FAULTS = ("F1", "F2", "F3", "F4", "F5")
FAULT_CONFIGS = {
    "F1": REPO / "configs/experiment/faults/f1-checkout-cpu.json",
    "F2": REPO / "configs/experiment/faults/f2-checkout-payment-latency.json",
    "F3": REPO / "configs/experiment/faults/f3-payment-unavailable.json",
    "F4": REPO / "configs/experiment/faults/f4-payment-charge-exception.json",
    "F5": REPO / "configs/experiment/faults/f5-redis-unavailable.json",
}
SUT = ("frontend", "checkoutservice", "cartservice", "productcatalogservice", "currencyservice",
       "paymentservice", "shippingservice", "emailservice", "recommendationservice", "adservice", "redis-cart")
OBS_DEPLOYMENTS = {
    "observability-conventional": ("prometheus-conventional", "loki-conventional", "jaeger-conventional"),
    "observability-otel": ("prometheus-otel", "loki-otel", "jaeger-otel", "otel-collector"),
}
GT_SCHEMA = REPO / "scripts/experiment/ground-truth/ground-truth.schema.json"
WORKLOAD_SCHEMA = REPO / "src/loadgenerator/experiment/run-metadata.schema.json"


class ValidationError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def marker_counts_match(source: dict[str, int], recorded: dict[str, int]) -> bool:
    keys = set(source) | set(recorded)
    return all(int(source.get(key, 0)) == int(recorded.get(key, 0)) for key in keys)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def select_faults(all_faults: bool, fault: str | None) -> tuple[str, ...]:
    if all_faults == (fault is not None):
        raise ValidationError("select exactly one of --all or --fault F1..F5")
    if all_faults:
        return FAULTS
    normalized = str(fault).upper()
    if normalized not in FAULTS:
        raise ValidationError("--fault must be one of F1, F2, F3, F4, F5")
    return (normalized,)


def validate_validation_id(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,32}", value):
        raise ValidationError("validation-id must be 1-33 lowercase DNS-safe characters")
    return value


def run_id_for(validation_id: str, fault: str) -> str:
    result = f"iv-{validation_id}-{fault.lower()}"
    if len(result) > 51:
        raise ValidationError("validation-id is too long to generate a valid experiment run ID")
    return result


def evidence_id_set(dataset: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    for signal in ("metrics", "logs", "traces"):
        for record in dataset.get(signal, []):
            if isinstance(record.get("evidence_id"), str):
                found.add(record["evidence_id"])
            if signal == "traces":
                found.update(span["evidence_id"] for span in record.get("spans", []) if isinstance(span.get("evidence_id"), str))
    return found


def check_window_chain(gt: dict, dataset_manifest: dict, dataset_a: dict, dataset_b: dict,
                       pre_buffer: float, post_buffer: float) -> dict[str, Any]:
    confirmed_start = parse_time(gt["timing"]["confirmed_start"])
    confirmed_end = parse_time(gt["timing"]["confirmed_end"])
    expected_start = confirmed_start - timedelta(seconds=pre_buffer)
    expected_end = confirmed_end + timedelta(seconds=post_buffer)
    manifest_window = dataset_manifest["window"]
    actual = {
        "manifest": {"start": manifest_window["start"], "end": manifest_window["end"]},
        "dataset_a": dataset_a["window"], "dataset_b": dataset_b["window"],
    }
    expected = {"start": iso_utc(expected_start), "end": iso_utc(expected_end)}
    match = all(actual[item] == expected for item in ("manifest", "dataset_a", "dataset_b"))
    return {
        "ground_truth": {"confirmed_start": gt["timing"]["confirmed_start"], "confirmed_end": gt["timing"]["confirmed_end"]},
        "pre_buffer_seconds": pre_buffer, "post_buffer_seconds": post_buffer,
        "expected_dataset_window": expected, "actual": actual, "match": match,
    }


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def evidence_comparison(dataset: dict, request: dict) -> dict[str, Any]:
    source_ids = evidence_id_set(dataset)
    request_ids = evidence_id_set(request["telemetry"])
    return {
        "dataset_ids": len(source_ids), "request_ids": len(request_ids),
        "dataset_minus_request": sorted(source_ids - request_ids),
        "request_minus_dataset": sorted(request_ids - source_ids),
        "match": source_ids == request_ids,
    }


def request_counts(dataset: dict, request: dict) -> dict[str, Any]:
    def counts(value: dict) -> dict[str, int]:
        return {
            "metrics": len(value.get("metrics", [])),
            "logs": len(value.get("logs", [])),
            "traces": len(value.get("traces", [])),
            "spans": sum(len(trace.get("spans", [])) for trace in value.get("traces", [])),
        }
    return {"dataset": counts(dataset), "request": counts(request["telemetry"]),
            "match": counts(dataset) == counts(request["telemetry"])}


def marker_key_paths(value: Any, path: str = "$") -> list[str]:
    hits: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = "".join(ch.lower() for ch in str(key) if ch.isalnum())
            child_path = f"{path}.{key}"
            if normalized == "telemetryscenario":
                hits.append(child_path)
            hits.extend(marker_key_paths(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            hits.extend(marker_key_paths(child, f"{path}[{index}]"))
    return hits


def expected_model_telemetry(dataset: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    """Apply only the explicit Phase 6 treatment-marker removal for audit comparison."""
    telemetry = deepcopy({signal: dataset[signal] for signal in ("metrics", "logs", "traces")})
    removed: dict[str, int] = {}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key in list(value):
                normalized = "".join(ch.lower() for ch in str(key) if ch.isalnum())
                if normalized == "telemetryscenario":
                    spelling = str(key)
                    removed[spelling] = removed.get(spelling, 0) + 1
                    del value[key]
                else:
                    visit(value[key])
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(telemetry)
    return telemetry, dict(sorted(removed.items()))


def recursive_key_paths(value: Any, terms: set[str], path: str = "$") -> dict[str, list[str]]:
    matches = {term: [] for term in terms}
    if isinstance(value, dict):
        for key, child in value.items():
            lower = str(key).lower()
            child_path = f"{path}.{key}"
            for term in terms:
                if lower == term.lower():
                    matches[term].append(child_path)
            nested = recursive_key_paths(child, terms, child_path)
            for term, paths in nested.items():
                matches[term].extend(paths)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            nested = recursive_key_paths(child, terms, f"{path}[{index}]")
            for term, paths in nested.items():
                matches[term].extend(paths)
    return matches


def validate_gt_document(gt: dict, fault: str, run_id: str) -> dict[str, Any]:
    schema = read_json(GT_SCHEMA)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(gt)
    expected_locations = {
        "F1": {"kind": "service", "component": "checkoutservice"},
        "F2": {"kind": "service_edge", "source": "checkoutservice", "target": "paymentservice"},
        "F3": {"kind": "service", "component": "paymentservice"},
        "F4": {"kind": "operation", "component": "paymentservice", "operation": "Charge"},
        "F5": {"kind": "datastore", "component": "redis-cart"},
    }
    start = gt.get("timing", {}).get("confirmed_start")
    end = gt.get("timing", {}).get("confirmed_end")
    checks = {
        "schema_valid": True,
        "run_id_matches": gt.get("run_id") == run_id,
        "status_valid": gt.get("status") == "valid",
        "injection_confirmed": gt.get("validation", {}).get("injection_confirmed") is True,
        "recovery_confirmed": gt.get("validation", {}).get("recovery_confirmed") is True,
        "confirmed_times_present": bool(start and end),
        "confirmed_end_after_start": bool(start and end and parse_time(end) > parse_time(start)),
        "fault_class_matches": gt.get("fault", {}).get("class") == fault,
        "expected_location_matches": all(
            gt.get("fault", {}).get("location", {}).get(key) == value
            for key, value in expected_locations[fault].items()
        ),
    }
    return {"checks": checks, "expected_location": expected_locations[fault],
            "actual_location": gt.get("fault", {}).get("location"),
            "timing": {key: gt.get("timing", {}).get(key) for key in
                       ("scheduled_start", "requested_at", "confirmed_start", "scheduled_end", "recovery_requested_at", "confirmed_end")},
            "passed": all(checks.values())}


class EvidenceRecorder:
    def __init__(self, root: Path):
        self.root = root
        self.commands_path = root / "commands.jsonl"
        self.counter = 0

    def command(self, argv: list[str], *, stage: str, fault: str | None = None,
                stdout_relative: str | None = None, stderr_relative: str | None = None,
                timeout: float = 120, check: bool = True, cwd: Path = REPO) -> subprocess.CompletedProcess:
        self.counter += 1
        slug = re.sub(r"[^a-z0-9-]+", "-", stage.lower()).strip("-")
        stdout_relative = stdout_relative or f"audit/commands/{self.counter:04d}-{slug}.stdout"
        stderr_relative = stderr_relative or f"audit/commands/{self.counter:04d}-{slug}.stderr"
        stdout_path, stderr_path = self.destination(stdout_relative), self.destination(stderr_relative)
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        started_at = utc_now()
        started = time.monotonic()
        result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
        duration = time.monotonic() - started
        stdout_path.write_text(result.stdout, encoding="utf-8")
        stderr_path.write_text(result.stderr, encoding="utf-8")
        record = {
            "timestamp": started_at, "finished_at": utc_now(), "stage": stage, "fault": fault,
            "command": argv, "cwd": str(cwd), "exit_code": result.returncode, "duration_seconds": round(duration, 3),
            "stdout_path": self.relative(stdout_path), "stderr_path": self.relative(stderr_path),
        }
        with self.commands_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
        if check and result.returncode != 0:
            raise ValidationError(f"command failed at {stage} (exit {result.returncode}): {' '.join(argv)}")
        return result

    def relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(REPO).as_posix()
        except ValueError:
            return path.as_posix()

    def destination(self, value: str) -> Path:
        path = Path(value)
        if path.is_absolute():
            return path
        if len(path.parts) >= 3 and path.parts[:2] == ("artifacts", "integration-validation"):
            return REPO / path
        return self.root / path


class IntegrationValidation:
    def __init__(self, validation_id: str, faults: tuple[str, ...], pre_buffer: float, post_buffer: float):
        self.validation_id = validate_validation_id(validation_id)
        self.faults = faults
        self.pre_buffer = pre_buffer
        self.post_buffer = post_buffer
        self.root = ARTIFACT_ROOT / validation_id
        if self.root.exists():
            raise ValidationError(f"validation artifact path already exists; refusing to overwrite: {self.root}")
        self.root.mkdir(parents=True)
        (self.root / "global").mkdir()
        self.recorder = EvidenceRecorder(self.root)
        self.campaign: dict[str, Any] = {
            "schema_version": "1.0", "validation_id": validation_id,
            "classification": "engineering_integration_validation_not_scientific_campaign",
            "started_at": utc_now(), "commit_initial": "edf2c893d37b2d3f52cc7652bb03427102977f1e",
            "fault_order": list(faults), "pre_buffer_seconds": pre_buffer, "post_buffer_seconds": post_buffer,
            "faults": {}, "global": {}, "warnings": [], "failures": [],
        }
        self.log_lines: list[str] = []

    def log(self, message: str) -> None:
        line = f"{utc_now()} {message}"
        self.log_lines.append(line)
        (self.root / "orchestration.log").write_text("\n".join(self.log_lines) + "\n", encoding="utf-8")

    def run(self) -> int:
        self.capture_environment()
        initial = self.capture_cluster_snapshot(self.root / "global/initial-state", "global-initial-state")
        preflight = self.global_preflight(initial)
        write_json(self.root / "global/preflight/result.json", preflight)
        self.campaign["global"]["preflight"] = {"passed": preflight["passed"], "evidence": ["global/preflight/result.json"]}
        if not preflight["passed"]:
            self.campaign["failures"].append({"stage": "global_preflight", "reasons": preflight["failures"], "evidence": ["global/preflight/result.json"]})
            self.finalize(initial)
            return 1
        self.run_observability("global/preflight/observability", None)
        preflight_observability = read_json(self.root / "global/preflight/observability/result.json")
        self.campaign["global"]["preflight"]["observability_passed"] = preflight_observability["passed"]
        if not preflight_observability["passed"]:
            self.campaign["failures"].append({"stage": "global_preflight", "reason": "Phase 2 observability validator failed", "evidence": ["global/preflight/observability/result.json"]})
            final = self.capture_cluster_snapshot(self.root / "global/final-state", "global-final-state")
            self.campaign["global"]["final_health"] = self.health_from_snapshot(final)
            self.campaign["global"]["final_observability"] = preflight_observability
            self.finalize(final)
            return 1
        for fault in self.faults:
            try:
                passed = self.run_fault(fault)
            except Exception as exc:
                fault_root = self.root / fault.lower()
                fault_root.mkdir(parents=True, exist_ok=True)
                failure = {"fault": fault, "run_id": run_id_for(self.validation_id, fault), "overall": "FAIL",
                           "statuses": {"fault_run": "FAIL"}, "failures": [{"stage": "control_plane_failed", "reason": f"{type(exc).__name__}: {exc}"}],
                           "warnings": [], "evidence": {}}
                self.write_fault_result(fault_root, failure)
                self.campaign["faults"][fault] = failure
                self.campaign["failures"].append({"fault": fault, **failure["failures"][0]})
                self.log(f"{fault} raised {type(exc).__name__}: {exc}")
                passed = False
            if not passed:
                if not self.safe_to_continue(fault):
                    self.campaign["aborted_after"] = fault
                    self.log(f"ABORT: unsafe residual fault state after {fault}; later faults were not started")
                    break
        final_snapshot = self.capture_cluster_snapshot(self.root / "global/final-state", "global-final-state")
        final_health = self.health_from_snapshot(final_snapshot)
        self.campaign["global"]["final_health"] = final_health
        self.campaign["global"]["final_observability"] = self.run_observability("global/final-state/observability", None)
        self.finalize(final_snapshot)
        return 0 if self.campaign["overall"] == "PASS" else 1

    def capture_environment(self) -> None:
        env: dict[str, Any] = {"captured_at": utc_now(), "repository": {}, "tools": {}}
        for name, argv in {
            "commit": ["git", "rev-parse", "HEAD"],
            "working_tree": ["git", "status", "--porcelain"],
        }.items():
            result = self.recorder.command(argv, stage=f"environment-{name}", stdout_relative=f"global/environment/{name}.txt")
            env["repository"][name] = result.stdout.strip()
        for name, argv in {
            "docker": ["docker", "version"],
            "kubectl": ["kubectl", "version", "-o", "json"],
        }.items():
            result = self.recorder.command(argv, stage=f"environment-{name}", timeout=60, check=False)
            try:
                observed = json.loads(result.stdout) if name == "kubectl" else result.stdout
            except json.JSONDecodeError:
                observed = result.stdout
            env["tools"][name] = {"exit_code": result.returncode, "output": observed}
        kind_bin = os.environ.get("KIND_BIN") or shutil.which("kind")
        if not kind_bin:
            fallback = Path("/tmp/tcc-tools/kind-v0.33.0/kind")
            if fallback.is_file():
                kind_bin = str(fallback)
        if kind_bin:
            env["tools"]["kind"] = self.recorder.command([kind_bin, "version"], stage="environment-kind").stdout.strip()
        else:
            env["tools"]["kind"] = {"available": False, "expected": "v0.33.0"}
        env["tools"]["chaos_mesh_version_expected"] = "2.8.6"
        write_json(self.root / "environment.json", env)

    def capture_cluster_snapshot(self, directory: Path, stage: str) -> dict[str, Any]:
        directory.mkdir(parents=True, exist_ok=True)
        commands = {
            "nodes": ["kubectl", "get", "nodes", "-o", "json"],
            "namespaces": ["kubectl", "get", "namespaces", "-o", "json"],
            "pods": ["kubectl", "get", "pods", "-A", "-o", "json"],
            "deployments": ["kubectl", "get", "deployments", "-A", "-o", "json"],
            "services": ["kubectl", "get", "services", "-A", "-o", "json"],
            "endpoints": ["kubectl", "get", "endpoints", "-A", "-o", "json"],
            "endpointslices": ["kubectl", "get", "endpointslices", "-A", "-o", "json"],
            "daemonsets": ["kubectl", "get", "daemonsets", "-A", "-o", "json"],
            "stresschaos": ["kubectl", "get", "stresschaos", "-A", "-o", "json"],
            "networkchaos": ["kubectl", "get", "networkchaos", "-A", "-o", "json"],
            "chaos-crds": ["kubectl", "get", "crd", "stresschaos.chaos-mesh.org", "networkchaos.chaos-mesh.org", "-o", "json"],
        }
        data: dict[str, Any] = {"captured_at": utc_now(), "artifacts": {}}
        for name, argv in commands.items():
            # Some kubectl installations lack a CRD before Chaos Mesh is installed.
            result = self.recorder.command(argv, stage=f"{stage}-{name}",
                                           stdout_relative=self.recorder.relative(directory / f"{name}.json"),
                                           check=False)
            if result.returncode == 0:
                try:
                    value = json.loads(result.stdout)
                except json.JSONDecodeError:
                    value = None
                data[name] = value
            else:
                data[name] = {"error": result.stderr.strip(), "exit_code": result.returncode}
            path = directory / f"{name}.json"
            data["artifacts"][name] = self.artifact(path)
        for name, argv in {
            "pods-wide.txt": ["kubectl", "get", "pods", "-A", "-o", "wide"],
            "deployments-wide.txt": ["kubectl", "get", "deployments", "-A", "-o", "wide"],
            "nodes-wide.txt": ["kubectl", "get", "nodes", "-o", "wide"],
        }.items():
            path = directory / name
            result = self.recorder.command(argv, stage=f"{stage}-{name[:-4]}",
                                           stdout_relative=self.recorder.relative(path))
            data.setdefault("human_readable", {})[name] = self.artifact(path)
        write_json(directory / "snapshot-summary.json", self._snapshot_summary(data))
        data["summary_path"] = self.recorder.relative(directory / "snapshot-summary.json")
        return data

    def _snapshot_summary(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        return {
            "captured_at": snapshot["captured_at"],
            "node_versions": [item.get("status", {}).get("nodeInfo", {}).get("kubeletVersion") for item in snapshot.get("nodes", {}).get("items", [])],
            "pod_count": len(snapshot.get("pods", {}).get("items", [])),
            "deployment_count": len(snapshot.get("deployments", {}).get("items", [])),
            "chaos_resources": {
                "StressChaos": len(snapshot.get("stresschaos", {}).get("items", [])),
                "NetworkChaos": len(snapshot.get("networkchaos", {}).get("items", [])),
            },
            "restarts": restart_counts(snapshot.get("pods", {}).get("items", [])),
        }

    def global_preflight(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        failures: list[str] = []
        nodes = snapshot.get("nodes", {}).get("items", [])
        ready_nodes = [node for node in nodes if any(c.get("type") == "Ready" and c.get("status") == "True" for c in node.get("status", {}).get("conditions", []))]
        versions = [node.get("status", {}).get("nodeInfo", {}).get("kubeletVersion", "unknown") for node in nodes]
        if not nodes or len(ready_nodes) != len(nodes): failures.append("kind nodes are not all Ready")
        if not versions or not all(re.match(r"v1\.35\.", version) for version in versions): failures.append(f"Kubernetes must be v1.35.x; observed {versions}")
        chaos_images = []
        chaos_controller_ready = False
        chaos_daemon_ready = False
        for item in snapshot.get("deployments", {}).get("items", []):
            metadata = item.get("metadata", {})
            if metadata.get("namespace") == "chaos-mesh" and metadata.get("name") == "chaos-controller-manager":
                desired = int(item.get("spec", {}).get("replicas", 1))
                ready = int(item.get("status", {}).get("readyReplicas", 0))
                chaos_controller_ready = desired > 0 and ready == desired
                chaos_images.extend(container.get("image") for container in item.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []))
        for item in snapshot.get("daemonsets", {}).get("items", []):
            metadata = item.get("metadata", {})
            if metadata.get("namespace") == "chaos-mesh" and metadata.get("name") == "chaos-daemon":
                desired = int(item.get("status", {}).get("desiredNumberScheduled", 0))
                ready = int(item.get("status", {}).get("numberReady", 0))
                chaos_daemon_ready = desired > 0 and ready == desired
                chaos_images.extend(container.get("image") for container in item.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []))
        chaos_version_ok = len(chaos_images) >= 2 and all("2.8.6" in str(image) for image in chaos_images)
        if not chaos_controller_ready or not chaos_daemon_ready:
            failures.append(f"Chaos Mesh controller/daemon not Ready: controller={chaos_controller_ready}, daemon={chaos_daemon_ready}")
        if not chaos_version_ok:
            failures.append(f"Chaos Mesh images must be 2.8.6; observed {chaos_images}")
        crd_names = {item.get("metadata", {}).get("name") for item in snapshot.get("chaos-crds", {}).get("items", [])}
        required_crds = {"stresschaos.chaos-mesh.org", "networkchaos.chaos-mesh.org"}
        chaos_crds_ok = crd_names == required_crds
        if not chaos_crds_ok:
            failures.append(f"Chaos Mesh fault CRDs missing: expected {sorted(required_crds)}, observed {sorted(crd_names)}")
        deployments = {(item.get("metadata", {}).get("namespace"), item.get("metadata", {}).get("name")): item for item in snapshot.get("deployments", {}).get("items", [])}
        required = [("default", service) for service in SUT]
        required += [(ns, name) for ns, names in OBS_DEPLOYMENTS.items() for name in names]
        deployment_checks = {}
        for namespace, name in required:
            item = deployments.get((namespace, name))
            spec = int(item.get("spec", {}).get("replicas", 1)) if item else 0
            ready = int(item.get("status", {}).get("readyReplicas", 0)) if item else 0
            ok = bool(item) and spec > 0 and ready == spec
            deployment_checks[f"{namespace}/{name}"] = {"desired": spec, "ready": ready, "passed": ok}
            if not ok: failures.append(f"deployment {namespace}/{name} Ready={ready}/{spec}")
        loadgen = deployments.get(("default", "loadgenerator"))
        loadgen_replicas = int(loadgen.get("spec", {}).get("replicas", 0)) if loadgen else None
        if loadgen_replicas != 0: failures.append(f"original loadgenerator replicas must be 0 (observed {loadgen_replicas})")
        daemon = next((item for item in snapshot.get("daemonsets", {}).get("items", []) if item.get("metadata", {}).get("namespace") == "observability-conventional" and item.get("metadata", {}).get("name") == "promtail-conventional"), None)
        promtail = {"desired": int(daemon.get("status", {}).get("desiredNumberScheduled", 0)) if daemon else 0,
                    "ready": int(daemon.get("status", {}).get("numberReady", 0)) if daemon else 0}
        promtail["passed"] = promtail["desired"] > 0 and promtail["ready"] == promtail["desired"]
        if not promtail["passed"]: failures.append(f"Promtail Ready={promtail['ready']}/{promtail['desired']}")
        for kind in ("stresschaos", "networkchaos"):
            items = snapshot.get(kind, {}).get("items", [])
            if items: failures.append(f"residual {kind} resources: {[item.get('metadata', {}).get('name') for item in items]}")
        pod_items = snapshot.get("pods", {}).get("items", [])
        payment_pods = [item for item in pod_items if item.get("metadata", {}).get("namespace") == "default" and item.get("metadata", {}).get("labels", {}).get("app") == "paymentservice"]
        flag_checks = []
        for pod in payment_pods:
            check = self.recorder.command(["kubectl", "exec", "-n", "default", pod["metadata"]["name"], "-c", "server", "--", "sh", "-c", "test ! -e /tmp/tcc-faults/payment-charge-exception"], stage="preflight-f4-flag", check=False)
            flag_checks.append({"pod": pod["metadata"]["name"], "exit_code": check.returncode, "absent": check.returncode == 0})
        if not flag_checks or any(not item["absent"] for item in flag_checks): failures.append("F4 control flag is not absent in every paymentservice pod")
        for target in ("paymentservice", "redis-cart"):
            item = deployments.get(("default", target))
            if not item or int(item.get("spec", {}).get("replicas", 0)) < 1:
                failures.append(f"fault target {target} is not at available baseline replicas")
        return {"checked_at": utc_now(), "kubernetes_versions": versions, "ready_nodes": len(ready_nodes),
                "deployments": deployment_checks, "original_loadgenerator_replicas": loadgen_replicas,
                "chaos_mesh": {"controller_ready": chaos_controller_ready, "daemon_ready": chaos_daemon_ready,
                               "images": chaos_images, "version_2_8_6": chaos_version_ok,
                               "crds": sorted(crd_names), "crds_ready": chaos_crds_ok},
                "promtail": promtail, "f4_flags": flag_checks,
                "residual_chaos": {name: len(snapshot.get(name, {}).get("items", [])) for name in ("stresschaos", "networkchaos")},
                "failures": failures, "passed": not failures,
                "evidence": [snapshot.get("summary_path"), "global/initial-state/snapshot-summary.json"]}

    def run_observability(self, directory: str, fault: str | None) -> dict[str, Any]:
        out = self.observability_path(directory, fault)
        out.mkdir(parents=True, exist_ok=True)
        start = utc_now()
        stage = "phase2-validator" if fault is None else f"{fault.lower()}-phase2-validator"
        result = self.recorder.command([str(REPO / "scripts/experiment/validate-observability.sh")], stage=stage,
                                       fault=fault, stdout_relative=self.recorder.relative(out / "stdout.txt"),
                                       stderr_relative=self.recorder.relative(out / "stderr.txt"), timeout=600, check=False)
        validation_json = extract_json_document(result.stdout)
        if validation_json:
            write_json(out / "validator-results.json", validation_json)
        record = {"started_at": start, "finished_at": utc_now(), "exit_code": result.returncode,
                  "passed": result.returncode == 0, "stdout": self.recorder.relative(out / "stdout.txt"),
                  "stderr": self.recorder.relative(out / "stderr.txt"),
                  "parsed_results": validation_json, "evidence": [self.recorder.relative(out / "stdout.txt"), self.recorder.relative(out / "stderr.txt")]}
        write_json(out / "result.json", record)
        return record

    def observability_path(self, directory: str, fault: str | None) -> Path:
        base = self.root if fault is None else self.root / fault.lower()
        return base / directory

    def run_fault(self, fault: str) -> bool:
        run_id = run_id_for(self.validation_id, fault)
        fault_root = self.root / fault.lower()
        fault_root.mkdir()
        run_dir = REPO / "artifacts/experiments" / run_id
        dataset_dir = REPO / "artifacts/datasets" / run_id
        rca_dir = REPO / "artifacts/rca-inputs" / run_id
        if any(path.exists() for path in (run_dir, dataset_dir, rca_dir)):
            failed = {"fault": fault, "run_id": run_id, "overall": "FAIL", "statuses": {"fault_run": "FAIL"},
                      "failures": [{"stage": "artifact_precondition", "reason": "run output already exists; refused overwrite"}],
                      "warnings": [], "evidence": {}}
            self.campaign["failures"].append({"fault": fault, **failed["failures"][0]})
            self.campaign["faults"][fault] = failed
            self.write_fault_result(fault_root, failed)
            return False
        config_path = FAULT_CONFIGS[fault]
        config_copy = fault_root / "fault-config.json"
        shutil.copyfile(config_path, config_copy)
        config_hash = sha256_file(config_copy)
        (fault_root / "run-id.txt").write_text(run_id + "\n", encoding="utf-8")
        before = self.capture_cluster_snapshot(fault_root / "audit/before", f"{fault.lower()}-before")
        self.capture_target_state(fault, run_id, fault_root / "audit/before")
        config_raw = read_json(config_copy)
        write_json(fault_root / "workload-parameters.json", {
            "executor": "host-wsl", "config_path": self.recorder.relative(config_copy), "config_sha256": config_hash,
            "workload_environment": config_raw["workload"], "pre_buffer_seconds": self.pre_buffer,
            "post_buffer_seconds": self.post_buffer,
        })
        events_path = run_dir / "orchestrator-events.jsonl"
        experiment_out, experiment_err = fault_root / "orchestrator.stdout", fault_root / "orchestrator.stderr"
        run_start = utc_now()
        run_started_mono = time.monotonic()
        command = [str(REPO / "scripts/experiment/run-experiment.sh"), "--config", str(config_copy), "--run-id", run_id, "--confirmation-timeout", "90"]
        stdout_stream = experiment_out.open("w", encoding="utf-8")
        stderr_stream = experiment_err.open("w", encoding="utf-8")
        proc = subprocess.Popen(command, cwd=REPO, stdout=stdout_stream, stderr=stderr_stream, text=True)
        command_record = {"timestamp": run_start, "stage": f"{fault.lower()}-fault-run", "fault": fault,
                          "command": command, "cwd": str(REPO), "pid": proc.pid,
                          "stdout_path": self.recorder.relative(experiment_out), "stderr_path": self.recorder.relative(experiment_err)}
        try:
            self.monitor_orchestrator(proc, events_path, fault, fault_root)
            run_code = proc.wait()
        finally:
            stdout_stream.close()
            stderr_stream.close()
        run_duration = time.monotonic() - run_started_mono
        command_record.update({"finished_at": utc_now(), "exit_code": run_code, "duration_seconds": round(run_duration, 3)})
        with self.recorder.commands_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(command_record, sort_keys=True) + "\n")
        gt_path = run_dir / "ground-truth.json"
        events = read_jsonl(events_path)
        if events_path.is_file():
            shutil.copyfile(events_path, fault_root / "orchestrator-events.jsonl")
        if experiment_out.is_file() and experiment_err.is_file():
            self.make_combined_log(experiment_out, experiment_err, fault_root / "orchestration.log")
        results: dict[str, Any] = {
            "fault": fault, "run_id": run_id, "config_path": self.recorder.relative(config_copy),
            "config_sha256": config_hash, "run_exit_code": run_code, "run_duration_seconds": round(run_duration, 3),
            "evidence": {"before_snapshot": self.recorder.relative(fault_root / "audit/before/snapshot-summary.json"),
                         "config": self.artifact(config_copy), "orchestration_stdout": self.artifact(experiment_out),
                         "orchestration_stderr": self.artifact(experiment_err)},
            "statuses": {}, "warnings": [], "failures": [],
        }
        if not gt_path.is_file():
            results["statuses"]["ground_truth"] = "FAIL"
            results["failures"].append({"stage": "ground_truth_failed", "reason": "ground-truth.json was not produced"})
        else:
            gt = read_json(gt_path)
            gt_validation = validate_gt_document(gt, fault, run_id)
            write_json(fault_root / "ground-truth-validation.json", gt_validation)
            results["statuses"]["ground_truth"] = "PASS" if gt_validation["passed"] else "FAIL"
            results["ground_truth"] = gt_validation
            results["evidence"]["ground_truth"] = self.artifact(gt_path)
            results["evidence"]["ground_truth_validation"] = self.artifact(fault_root / "ground-truth-validation.json")
        results["timeline"] = self.timeline(events, fault_root, run_dir)
        results["workload"] = self.audit_workload(run_dir, run_id, fault_root, results)
        if run_code != 0:
            results["statuses"]["fault_run"] = "FAIL"
            results["failures"].append({"stage": "fault_run_failed", "reason": f"orchestrator exit code {run_code}"})
        else:
            results["statuses"]["fault_run"] = "PASS"
        if not results["workload"].get("passed"):
            results["statuses"]["fault_run"] = "FAIL"
        results["injection"] = self.audit_injection(fault, fault_root, gt_path)
        results["statuses"]["injection"] = "PASS" if results["injection"]["passed"] else "FAIL"
        if not results["injection"]["passed"]:
            results["failures"].append({"stage": "injection_failed", "reason": "direct during-fault state did not match the configured mechanism", "checks": results["injection"]["checks"]})
        results["statuses"]["recovery"] = self.audit_recovery(fault, fault_root, gt_path, run_dir, events)
        results["state_comparison"] = self.state_comparison(fault, fault_root)
        self.capture_cluster_snapshot(fault_root / "audit/after", f"{fault.lower()}-after")
        self.capture_target_state(fault, run_id, fault_root / "audit/after")
        self.run_observability(f"audit/observability", fault)
        obs = read_json(fault_root / "audit/observability/result.json")
        results["statuses"]["observability"] = "PASS" if obs["passed"] else "FAIL"
        results["observability"] = obs
        self.collect_dataset_and_request(fault, run_id, fault_root, results)
        self.validate_cleanup(fault, fault_root, results)
        results["overall"] = "PASS" if all(results["statuses"].get(key) == "PASS" for key in
                                             ("fault_run", "workload", "ground_truth", "injection", "recovery", "observability", "dataset", "rca_request", "provenance", "cleanup")) else "FAIL"
        if results["overall"] != "PASS":
            self.campaign["failures"].extend({"fault": fault, **entry} for entry in results["failures"])
        self.campaign["warnings"].extend({"fault": fault, **entry} for entry in results["warnings"])
        self.campaign["faults"][fault] = results
        self.write_fault_result(fault_root, results)
        self.write_fault_summary(fault_root, results)
        self.log(f"{fault} {run_id}: {results['overall']}; statuses={results['statuses']}")
        return results["overall"] == "PASS"

    def monitor_orchestrator(self, proc: subprocess.Popen, events_path: Path, fault: str, fault_root: Path) -> None:
        seen = 0
        while proc.poll() is None:
            events = read_jsonl(events_path)
            for event in events[seen:]:
                event_name = event.get("event")
                self.log(f"{fault} event: {event_name} at {event.get('timestamp')}")
                if event_name == "fault_requested":
                    self.capture_target_state(fault, event.get("run_id", ""), fault_root / "audit/injection-requested")
                elif event_name == "fault_confirmed":
                    write_json(fault_root / "audit/injection/fault-confirmation-event.json", event)
                    if fault in ("F1", "F2"):
                        evidence = event.get("evidence", {})
                        kind = "stresschaos" if fault == "F1" else "networkchaos"
                        resource_name = evidence.get("name")
                        if resource_name:
                            self.recorder.command(["kubectl", "get", kind, resource_name, "-n", "default", "-o", "json"],
                                                  stage=f"{fault.lower()}-confirmed-chaos-resource", fault=fault, check=False,
                                                  stdout_relative=self.recorder.relative(fault_root / "audit/injection/confirmed-chaos-resource.json"))
                    self.capture_confirmed_state(fault, event, fault_root / "audit/injection", "injection")
                elif event_name == "recovery_requested":
                    self.capture_target_state(fault, event.get("run_id", ""), fault_root / "audit/recovery-requested")
                elif event_name == "recovery_confirmed":
                    write_json(fault_root / "audit/recovery/recovery-confirmation-event.json", event)
                    self.capture_confirmed_state(fault, event, fault_root / "audit/recovery", "recovery")
                seen += 1
            time.sleep(0.1)
        for event in read_jsonl(events_path)[seen:]:
            self.log(f"{fault} event: {event.get('event')} at {event.get('timestamp')}")

    def capture_confirmed_state(self, fault: str, event: dict[str, Any], directory: Path, phase: str) -> None:
        # Capture the fault target immediately at the transition. Cluster-wide
        # snapshots are slower and can outlast short fault windows (notably F4).
        self.capture_target_state(fault, event.get("run_id", ""), directory)
        self.capture_cluster_snapshot(directory, f"{fault.lower()}-{phase}")

    def capture_target_state(self, fault: str, run_id: str, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        deploy_name = "checkoutservice" if fault == "F1" else "paymentservice" if fault in ("F2", "F3", "F4") else "redis-cart"
        commands = {
            "deployment.json": ["kubectl", "get", "deployment", deploy_name, "-n", "default", "-o", "json"],
            "pods.json": ["kubectl", "get", "pods", "-n", "default", "-l", f"app={deploy_name}", "-o", "json"],
            "endpoints.json": ["kubectl", "get", "endpoints", deploy_name, "-n", "default", "-o", "json"],
            "endpointslices.json": ["kubectl", "get", "endpointslices", "-n", "default", "-l", f"kubernetes.io/service-name={deploy_name}", "-o", "json"],
        }
        for filename, argv in commands.items():
            path = directory / filename
            self.recorder.command(argv, stage=f"{fault.lower()}-{directory.name}-{filename[:-5]}", fault=fault,
                                  stdout_relative=self.recorder.relative(path), check=False)
        if fault == "F2":
            for filename, argv in {
                "source-deployment.json": ["kubectl", "get", "deployment", "checkoutservice", "-n", "default", "-o", "json"],
                "source-pods.json": ["kubectl", "get", "pods", "-n", "default", "-l", "app=checkoutservice", "-o", "json"],
            }.items():
                self.recorder.command(argv, stage=f"f2-{directory.name}-{filename[:-5]}", fault=fault,
                                      stdout_relative=self.recorder.relative(directory / filename), check=False)
        if fault in ("F1", "F2") and directory.name in ("injection", "recovery"):
            resource = "stresschaos" if fault == "F1" else "networkchaos"
            gt_path = REPO / "artifacts/experiments" / run_id / "ground-truth.json"
            gt = read_json(gt_path) if gt_path.is_file() else {}
            name = gt.get("validation", {}).get("mechanism_confirmation", {}).get("apply_request", {}).get("resource")
            if name:
                self.recorder.command(["kubectl", "get", resource, name, "-n", "default", "-o", "json"], stage=f"{fault.lower()}-chaos-object", fault=fault,
                                      stdout_relative=self.recorder.relative(directory / f"{resource}.json"), check=False)
            else:
                self.recorder.command(["kubectl", "get", resource, "-A", "-o", "json"], stage=f"{fault.lower()}-chaos-list", fault=fault,
                                      stdout_relative=self.recorder.relative(directory / f"{resource}.json"), check=False)
        if fault == "F4":
            pods_path = directory / "pods.json"
            pods = read_json(pods_path).get("items", []) if pods_path.is_file() else []
            for pod in pods:
                self.recorder.command(["kubectl", "exec", "-n", "default", pod["metadata"]["name"], "-c", "server", "--", "sh", "-c",
                                       "if test -e /tmp/tcc-faults/payment-charge-exception; then printf present; else printf absent; fi"],
                                      stage=f"f4-flag-{directory.name}", fault="F4", check=False,
                                      stdout_relative=self.recorder.relative(directory / f"flag-{pod['metadata']['name']}.txt"))

    def audit_workload(self, run_dir: Path, run_id: str, fault_root: Path, result: dict) -> dict[str, Any]:
        metadata_path = run_dir / "run-metadata.json"
        log_path = run_dir / "workload-runner.log"
        if not metadata_path.is_file():
            result["statuses"]["workload"] = "FAIL"
            result["failures"].append({"stage": "workload_metadata", "reason": "run-metadata.json missing"})
            return {"metadata": None, "evidence": []}
        metadata = read_json(metadata_path)
        schema = read_json(WORKLOAD_SCHEMA)
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(metadata)
        values = {key: metadata.get(key) for key in (
            "workload_model", "target_flow_rate_per_second", "max_in_flight_flows", "warmup_seconds_configured",
            "duration_seconds_configured", "cooldown_seconds_configured", "scheduled_flows", "started_flows",
            "completed_flows", "failed_flows", "control_plane_dropped_flows", "control_plane_drops_by_reason",
            "start_lag_ms", "observed_flow_start_rate_per_second", "observed_flow_completion_rate_per_second",
            "observed_requests_per_second", "requests", "failures_by_stage", "result",
        )}
        drops_by_reason = metadata.get("control_plane_drops_by_reason", {})
        scheduler_late = int(drops_by_reason.get("scheduler_late", 0))
        passed = (metadata.get("run_id") == run_id and metadata.get("workload_model") == "open-loop"
                  and int(metadata.get("started_flows", 0)) > 0
                  and int(metadata.get("control_plane_dropped_flows", 0)) == 0 and scheduler_late == 0
                  and metadata.get("result") not in ("running", "failed"))
        result["statuses"]["workload"] = "PASS" if passed else "FAIL"
        result.setdefault("statuses", {})
        result["workload"] = {"values": values, "metadata_path": self.recorder.relative(metadata_path),
                               "workload_log_path": self.recorder.relative(log_path), "metadata_sha256": sha256_file(metadata_path),
                               "scheduler_late_drops": scheduler_late,
                               "workload_log_sha256": sha256_file(log_path) if log_path.is_file() else None,
                               "passed": passed}
        result["evidence"]["run_metadata"] = self.artifact(metadata_path)
        if log_path.is_file(): result["evidence"]["workload_log"] = self.artifact(log_path)
        # Workload validity is a prerequisite of the fault run status.
        if not passed:
            result["statuses"]["fault_run"] = "FAIL"
            result["failures"].append({"stage": "invalid_workload", "reason": "metadata is invalid, not open-loop, or reports generator drops"})
        return result["workload"]

    def audit_injection(self, fault: str, fault_root: Path, gt_path: Path) -> dict[str, Any]:
        directory = fault_root / "audit/injection"
        checks: dict[str, bool] = {}
        observed: dict[str, Any] = {}
        if fault in ("F3", "F5"):
            deployment = json_load_optional(directory / "deployment.json", {})
            endpoints = json_load_optional(directory / "endpoints.json", {})
            confirmation = json_load_optional(directory / "fault-confirmation-event.json", {})
            confirmed = confirmation.get("evidence", {})
            desired = int(deployment.get("spec", {}).get("replicas", -1))
            ready = int(deployment.get("status", {}).get("readyReplicas", 0))
            endpoint_count = endpoint_addresses(endpoints)
            observed = {"desired_replicas": desired, "ready_replicas": ready, "endpoint_addresses": endpoint_count,
                        "confirmed_mechanism_evidence": confirmed}
            checks = {"scaled_to_zero": desired == 0 and confirmed.get("desired_replicas") == 0,
                      "ready_zero": ready == 0 and confirmed.get("ready_replicas") == 0,
                      "endpoints_zero": endpoint_count == 0 and confirmed.get("service_endpoints") == 0}
        elif fault == "F4":
            pods = json_load_optional(directory / "pods.json", {}).get("items", [])
            flags = sorted(directory.glob("flag-*.txt"))
            flag_states = {path.stem.removeprefix("flag-"): path.read_text(encoding="utf-8").strip() for path in flags}
            deployment = json_load_optional(directory / "deployment.json", {})
            ready = int(deployment.get("status", {}).get("readyReplicas", 0))
            desired = int(deployment.get("spec", {}).get("replicas", 0))
            before = json_load_optional(fault_root / "audit/before/pods.json", {}).get("items", [])
            before_by_name = {p.get("metadata", {}).get("name"): (p.get("metadata", {}).get("uid"), restart_count_pod(p)) for p in before}
            during_by_name = {p.get("metadata", {}).get("name"): (p.get("metadata", {}).get("uid"), restart_count_pod(p)) for p in pods}
            gt = json_load_optional(gt_path, {})
            effect = gt.get("validation", {}).get("fault_effect_observed") is True
            confirmation = json_load_optional(directory / "fault-confirmation-event.json", {})
            confirmation_flags = confirmation.get("evidence", {}).get("flags_present_by_pod", {})
            observed = {"flags_by_pod": flag_states, "ready_replicas": ready, "desired_replicas": desired,
                        "pod_uid_restart_before": before_by_name, "pod_uid_restart_during": during_by_name,
                        "fault_effect_observed_in_workload": effect, "confirmation_event": confirmation}
            checks = {"flag_present_all_pods": bool(confirmation_flags) and all(confirmation_flags.values()) and bool(flag_states) and all(v == "present" for v in flag_states.values()),
                      "service_still_ready": desired > 0 and ready == desired,
                      "same_pods_and_restart_counts": before_by_name == during_by_name,
                      "charge_error_observed": effect}
        else:
            resource_kind = "stresschaos" if fault == "F1" else "networkchaos"
            confirmed_resource = json_load_optional(directory / "confirmed-chaos-resource.json", {})
            resource_doc = json_load_optional(directory / f"{resource_kind}.json", {})
            items = resource_doc.get("items", []) if isinstance(resource_doc, dict) else []
            gt = json_load_optional(gt_path, {})
            resource_name = gt.get("validation", {}).get("mechanism_confirmation", {}).get("apply_request", {}).get("resource")
            item = confirmed_resource if confirmed_resource.get("kind") == ("StressChaos" if fault == "F1" else "NetworkChaos") else next(
                (item for item in items if item.get("metadata", {}).get("name") == resource_name), items[0] if len(items) == 1 else {})
            spec = item.get("spec", {})
            conditions = item.get("status", {}).get("conditions", [])
            condition_map = {condition.get("type"): str(condition.get("status", "")).lower() == "true" for condition in conditions}
            deployment = json_load_optional(directory / "deployment.json", {})
            pods = json_load_optional(directory / "pods.json", {}).get("items", [])
            before = json_load_optional(fault_root / "audit/before/pods.json", {}).get("items", [])
            before_restarts = {p.get("metadata", {}).get("uid"): restart_count_pod(p) for p in before}
            during_restarts = {p.get("metadata", {}).get("uid"): restart_count_pod(p) for p in pods}
            ready = int(deployment.get("status", {}).get("readyReplicas", 0))
            desired = int(deployment.get("spec", {}).get("replicas", 0))
            observed = {"resource_name": item.get("metadata", {}).get("name"), "kind": item.get("kind"),
                        "spec": spec, "conditions": conditions, "ready_replicas": ready, "desired_replicas": desired,
                        "restart_counts_before": before_restarts, "restart_counts_during": during_restarts}
            checks = {"resource_exists": bool(item), "selected": condition_map.get("Selected", False),
                      "all_injected": condition_map.get("AllInjected", False), "service_ready": ready == desired and desired > 0,
                      "restart_counts_unchanged": before_restarts == during_restarts}
            if fault == "F2":
                payment = deployment
                checkout = json_load_optional(fault_root / "audit/injection/source-deployment.json", {})
                checks["paymentservice_ready"] = int(payment.get("status", {}).get("readyReplicas", 0)) == int(payment.get("spec", {}).get("replicas", 0)) > 0
                checks["checkoutservice_ready"] = int(checkout.get("status", {}).get("readyReplicas", 0)) == int(checkout.get("spec", {}).get("replicas", 0)) > 0
            selector = spec.get("selector", {})
            if fault == "F1":
                checks["checkoutservice_target"] = selector.get("labelSelectors", {}).get("app") == "checkoutservice"
                observed["parameters_applied"] = {"workers": spec.get("stressors", {}).get("cpu", {}).get("workers"),
                                                   "load": spec.get("stressors", {}).get("cpu", {}).get("load")}
            else:
                target = spec.get("target", {}).get("selector", {}).get("labelSelectors", {})
                checks["edge_checkout_to_payment"] = selector.get("labelSelectors", {}).get("app") == "checkoutservice" and target.get("app") == "paymentservice" and spec.get("direction") == "to"
                observed["edge"] = {"direction": spec.get("direction"), "source": selector, "target": spec.get("target", {}).get("selector"), "delay": spec.get("delay")}
        return {"fault": fault, "checked_at": utc_now(), "observed": observed, "checks": checks,
                "passed": bool(checks) and all(checks.values()),
                "evidence": [self.recorder.relative(directory / name) for name in
                             ("deployment.json", "pods.json", "endpoints.json", "stresschaos.json", "networkchaos.json")
                             if (directory / name).is_file()]}

    def audit_recovery(self, fault: str, fault_root: Path, gt_path: Path, run_dir: Path, events: list[dict]) -> str:
        recovery_snapshot = fault_root / "audit/recovery/snapshot-summary.json"
        status = "FAIL"
        checks: dict[str, Any] = {"ground_truth_recovery_confirmed": False, "snapshot_exists": recovery_snapshot.is_file()}
        if gt_path.is_file():
            gt = read_json(gt_path)
            checks["ground_truth_recovery_confirmed"] = gt.get("validation", {}).get("recovery_confirmed") is True
        if fault in ("F3", "F5") and recovery_snapshot.is_file():
            deployment = json_load_optional(fault_root / "audit/recovery/deployment.json", {})
            endpoints = json_load_optional(fault_root / "audit/recovery/endpoints.json", {})
            event = json_load_optional(fault_root / "audit/recovery/recovery-confirmation-event.json", {})
            confirmed = event.get("evidence", {})
            checks["restored_original_replicas"] = int(deployment.get("spec", {}).get("replicas", 0)) == int(json_load_optional(fault_root / "audit/before/deployment.json", {}).get("spec", {}).get("replicas", -1))
            checks["ready_replicas_restored"] = int(deployment.get("status", {}).get("readyReplicas", 0)) == int(deployment.get("spec", {}).get("replicas", -1)) > 0
            checks["endpoints_restored"] = endpoint_addresses(endpoints) > 0
            checks["injector_confirmation_ready"] = bool(confirmed.get("ready")) and int(confirmed.get("ready_replicas", 0)) > 0
        elif fault == "F4" and recovery_snapshot.is_file():
            gt = read_json(gt_path) if gt_path.is_file() else {}
            validations = gt.get("validation", {})
            check_files = sorted((fault_root / "audit/recovery").glob("flag-*.txt"))
            flags_absent = bool(check_files) and all(path.read_text(encoding="utf-8").strip() == "absent" for path in check_files)
            checks["charge_error_observed_while_active"] = validations.get("fault_effect_observed") is True
            checks["charge_succeeded_after_recovery"] = validations.get("post_recovery_success_observed") is True
            checks["flag_absent_after_recovery"] = flags_absent
            before = json_load_optional(fault_root / "audit/before/pods.json", {}).get("items", [])
            after = json_load_optional(fault_root / "audit/recovery/pods.json", {}).get("items", [])
            before_by_uid = {pod.get("metadata", {}).get("uid"): restart_count_pod(pod) for pod in before}
            after_by_uid = {pod.get("metadata", {}).get("uid"): restart_count_pod(pod) for pod in after}
            checks["same_pod_uids_and_restart_counts"] = bool(before_by_uid) and before_by_uid == after_by_uid
        else:
            checks["snapshot_exists"] = recovery_snapshot.is_file()
            if fault in ("F1", "F2") and recovery_snapshot.is_file():
                snapshot = json_load_optional(recovery_snapshot, {})
                residual = snapshot.get("chaos_resources", {})
                deploy_name = "checkoutservice" if fault == "F1" else "paymentservice"
                deployment = json_load_optional(fault_root / "audit/recovery/deployment.json", {})
                checks["chaos_resources_removed"] = int(residual.get("StressChaos", 0)) == 0 and int(residual.get("NetworkChaos", 0)) == 0
                checks["target_ready_after_recovery"] = int(deployment.get("status", {}).get("readyReplicas", 0)) > 0
                if fault == "F1":
                    before = json_load_optional(fault_root / "audit/before/pods.json", {}).get("items", [])
                    after = json_load_optional(fault_root / "audit/recovery/pods.json", {}).get("items", [])
                    before_map = {pod.get("metadata", {}).get("uid"): restart_count_pod(pod) for pod in before}
                    after_map = {pod.get("metadata", {}).get("uid"): restart_count_pod(pod) for pod in after}
                    checks["checkout_pod_restarts_unchanged"] = bool(before_map) and before_map == after_map
                if fault == "F2":
                    payment = deployment
                    checkout = json_load_optional(fault_root / "audit/recovery/source-deployment.json", {})
                    checks["paymentservice_ready_after_recovery"] = int(payment.get("status", {}).get("readyReplicas", 0)) == int(payment.get("spec", {}).get("replicas", 0)) > 0
                    checks["checkoutservice_ready_after_recovery"] = int(checkout.get("status", {}).get("readyReplicas", 0)) == int(checkout.get("spec", {}).get("replicas", 0)) > 0
        status = "PASS" if checks and all(checks.values()) else "FAIL"
        write_json(fault_root / "audit/recovery/recovery-check.json", {"fault": fault, "checked_at": utc_now(), "checks": checks, "status": status})
        return status

    def collect_dataset_and_request(self, fault: str, run_id: str, fault_root: Path, result: dict[str, Any]) -> None:
        dataset_dir = REPO / "artifacts/datasets" / run_id
        run_dir = REPO / "artifacts/experiments" / run_id
        gt_path = run_dir / "ground-truth.json"
        if result["statuses"].get("ground_truth") != "PASS" or result["statuses"].get("recovery") != "PASS":
            result["statuses"]["dataset"] = "SKIPPED"
            result["statuses"]["rca_request"] = "SKIPPED"
            result["statuses"]["provenance"] = "FAIL"
            result["failures"].append({"stage": "provenance_failed", "reason": "invalid GT/recovery; refused official dataset and request generation"})
            return
        dataset_cmd = [str(REPO / "scripts/experiment/build-dataset.sh"), "build", "--run-id", run_id,
                       "--pre-buffer", str(self.pre_buffer), "--post-buffer", str(self.post_buffer)]
        result["timeline"]["dataset_collection_started"] = utc_now()
        dataset_sub = self.recorder.command(dataset_cmd, stage=f"{fault.lower()}-dataset-build", fault=fault, timeout=900, check=False,
                                            stdout_relative=self.recorder.relative(fault_root / "audit/dataset/stdout.txt"),
                                            stderr_relative=self.recorder.relative(fault_root / "audit/dataset/stderr.txt"))
        result["timeline"]["dataset_collection_finished"] = utc_now()
        if dataset_sub.returncode != 0 or not (dataset_dir / "manifest.json").is_file():
            result["statuses"]["dataset"] = "FAIL"
            result["statuses"]["rca_request"] = "SKIPPED"
            result["statuses"]["provenance"] = "FAIL"
            result["failures"].append({"stage": "dataset_failed", "reason": f"Dataset Builder exited {dataset_sub.returncode}"})
            return
        try:
            dataset_manifest = read_json(dataset_dir / "manifest.json")
            datasets = {s: read_json(dataset_dir / f"scenario-{s.lower()}" / "dataset.json") for s in ("A", "B")}
            stats = {s: read_json(dataset_dir / f"scenario-{s.lower()}" / "stats.json") for s in ("A", "B")}
            window_audit = check_window_chain(read_json(gt_path), dataset_manifest, datasets["A"], datasets["B"], self.pre_buffer, self.post_buffer)
            query_audit = self.audit_queries(dataset_dir, dataset_manifest, datasets)
            dataset_blindness = self.audit_dataset_blindness(datasets, run_id, fault_root)
            correlation = {"A": {"logs": len(datasets["A"]["logs"]), "with_trace_id": sum(log.get("trace_id") is not None for log in datasets["A"]["logs"]), "with_span_id": sum(log.get("span_id") is not None for log in datasets["A"]["logs"])},
                           "B": {"logs": len(datasets["B"]["logs"]), "with_trace_id": sum(log.get("trace_id") is not None for log in datasets["B"]["logs"]), "with_span_id": sum(log.get("span_id") is not None for log in datasets["B"]["logs"])} }
            dataset_checks = {
                "manifest_normalized": dataset_manifest.get("status") == "normalized",
                "same_window": datasets["A"]["window"] == datasets["B"]["window"],
                "window_chain": window_audit["match"], "queries_same_window_and_global_scope": query_audit["passed"],
                "global_sut_scope": all(set(datasets[s].get("services", [])) == set(SUT) for s in ("A", "B")),
                "A_cross_signal_correlation_zero": correlation["A"]["with_trace_id"] == 0 and correlation["A"]["with_span_id"] == 0,
                "no_control_plane_keys": dataset_blindness["passed"],
            }
            write_json(fault_root / "audit/dataset/window-chain.json", window_audit)
            write_json(fault_root / "audit/dataset/query-metadata.json", query_audit)
            write_json(fault_root / "audit/dataset/blindness.json", dataset_blindness)
            write_json(fault_root / "audit/dataset/correlation.json", correlation)
            dataset_evidence = {s: self.dataset_signal_summary(dataset_dir, s, stats[s]) for s in ("A", "B")}
            dataset_hashes_before_request = {s: sha256_file(dataset_dir / f"scenario-{s.lower()}/dataset.json") for s in ("A", "B")}
            for scenario in ("A", "B"):
                self.write_json_reference(fault_root / f"audit/dataset/dataset-{scenario.lower()}-summary.json", dataset_evidence[scenario])
            dataset_pass = all(dataset_checks.values())
            result["statuses"]["dataset"] = "PASS" if dataset_pass else "FAIL"
            result["dataset"] = {"checks": dataset_checks, "window_chain": window_audit, "queries": query_audit,
                                 "blindness": dataset_blindness, "correlation": correlation,
                                 "scenarios": dataset_evidence, "manifest": self.artifact(dataset_dir / "manifest.json")}
            if not dataset_pass:
                result["failures"].append({"stage": "dataset_failed", "reason": "one or more dataset invariants failed", "checks": dataset_checks})
            rca_cmd = [str(REPO / "scripts/experiment/build-rca-input.sh"), "--run-id", run_id]
            result["timeline"]["rca_request_generated_started"] = utc_now()
            rca_sub = self.recorder.command(rca_cmd, stage=f"{fault.lower()}-rca-build", fault=fault, timeout=900, check=False,
                                             stdout_relative=self.recorder.relative(fault_root / "audit/rca-request/stdout.txt"),
                                             stderr_relative=self.recorder.relative(fault_root / "audit/rca-request/stderr.txt"))
            result["timeline"]["rca_request_generated"] = utc_now()
            if rca_sub.returncode != 0:
                result["statuses"]["rca_request"] = "FAIL"
                result["statuses"]["provenance"] = "FAIL"
                result["failures"].append({"stage": "rca_request_failed", "reason": f"RCA input builder exited {rca_sub.returncode}"})
                return
            request_root = REPO / "artifacts/rca-inputs" / run_id
            request_a = read_json(request_root / "scenario-a/request.json")
            request_b = read_json(request_root / "scenario-b/request.json")
            rca_manifests = {s: read_json(request_root / f"scenario-{s.lower()}/manifest.json") for s in ("A", "B")}
            request_audit = self.audit_requests(run_id, fault_root, datasets, dataset_dir, request_root, request_a, request_b, rca_manifests)
            request_checks = dict(request_audit["checks"])
            request_checks["datasets_unchanged_during_request_build"] = all(
                dataset_hashes_before_request[s] == sha256_file(dataset_dir / f"scenario-{s.lower()}/dataset.json") for s in ("A", "B")
            )
            request_audit["checks"] = request_checks
            result["statuses"]["rca_request"] = "PASS" if all(request_checks.values()) else "FAIL"
            provenance_checks = {
                "source_dataset_hash_matches_request_manifest": all(sha256_file(dataset_dir / f"scenario-{s.lower()}/dataset.json") == rca_manifests[s]["source_dataset_sha256"] for s in ("A", "B")),
                "request_file_hash_matches_request_manifest": all(sha256_file(request_root / f"scenario-{s.lower()}/request.json") == rca_manifests[s]["request_sha256"] for s in ("A", "B")),
                "request_sanitization_logged": all("model_visible_sanitization" in rca_manifests[s] for s in ("A", "B")),
                "datasets_unchanged_during_request_build": request_checks["datasets_unchanged_during_request_build"],
            }
            result["statuses"]["provenance"] = "PASS" if all(provenance_checks.values()) else "FAIL"
            result["request"] = request_audit
            result["provenance"] = {"checks": provenance_checks, "request_manifests": {s: self.artifact(request_root / f"scenario-{s.lower()}/manifest.json") for s in ("A", "B")}}
            write_json(fault_root / "audit/rca-request/audit.json", {"request_audit": request_audit, "provenance": result["provenance"]})
            if result["statuses"]["rca_request"] != "PASS" or result["statuses"]["provenance"] != "PASS":
                result["failures"].append({"stage": "provenance_failed", "reason": "Dataset-to-request identity or completeness validation failed"})
        except Exception as exc:
            result["statuses"]["dataset"] = "FAIL"
            result["statuses"]["rca_request"] = "FAIL"
            result["statuses"]["provenance"] = "FAIL"
            result["failures"].append({"stage": "dataset_failed", "reason": f"{type(exc).__name__}: {exc}"})

    def audit_queries(self, dataset_dir: Path, manifest: dict, datasets: dict) -> dict[str, Any]:
        queries: dict[str, Any] = {}
        for scenario in ("A", "B"):
            queries[scenario] = {}
            for signal in ("metrics", "logs", "traces"):
                raw_path = dataset_dir / f"scenario-{scenario.lower()}/raw/{signal}.json"
                raw = read_json(raw_path)
                query = raw.get("query", {})
                queries[scenario][signal] = {
                    "backend": raw.get("source"), "query_start": query.get("window", {}).get("start"),
                    "query_end": query.get("window", {}).get("end"), "selector": query.get("params", {}).get("query") or query.get("search"),
                    "http_status": query.get("http_status"), "records_returned": query.get("records_returned"),
                    "samples_returned": query.get("samples_returned"), "page_count": query.get("page_count"),
                    "page_http_statuses": [page.get("http_status") for page in query.get("pages", [])],
                    "jaeger_searches": query.get("searches"), "services_queried": query.get("service_discovery", {}).get("sut_services_available"),
                    "raw_path": self.recorder.relative(raw_path),
                    "raw_sha256": sha256_file(raw_path), "raw_bytes": raw_path.stat().st_size,
                }
        expected = manifest["window"]
        query_meta_same = True
        allowlist_match = True
        expected_services_regex = "^(" + "|".join(SUT) + ")$"
        for scenario in ("A", "B"):
            for signal in ("metrics", "logs", "traces"):
                query = queries[scenario][signal]
                if query["query_start"] != expected["start"] or query["query_end"] != expected["end"]:
                    query_meta_same = False
                if signal == "traces":
                    # Jaeger encodes each actual request in Unix microseconds.
                    start_us = int(parse_time(expected["start"]).timestamp() * 1_000_000)
                    end_us = int(parse_time(expected["end"]).timestamp() * 1_000_000)
                    searches = query.get("jaeger_searches") or []
                    if not searches or any(int(item.get("params", {}).get("start", -1)) != start_us or
                                           int(item.get("params", {}).get("end", -1)) != end_us or
                                           item.get("http_status") != 200 for item in searches):
                        query_meta_same = False
                    if set(query.get("services_queried") or []) - set(SUT):
                        allowlist_match = False
                    if not query.get("services_queried"):
                        allowlist_match = False
                elif signal == "logs":
                    if expected_services_regex not in str(query.get("selector", "")):
                        allowlist_match = False
                    statuses = query.get("page_http_statuses", [])
                    if (query.get("http_status") not in (200, None) or not statuses or
                            any(status != 200 for status in statuses)):
                        query_meta_same = False
                elif query.get("http_status") != 200:
                    query_meta_same = False
                elif expected_services_regex not in str(query.get("selector", "")):
                    allowlist_match = False
        normalized_window_same = datasets["A"]["window"] == datasets["B"]["window"] == {"start": expected["start"], "end": expected["end"]}
        audit = {"queries": queries, "expected_window": expected, "backend_query_window_match": query_meta_same,
                 "global_sut_allowlist_match": allowlist_match, "fixed_sut_allowlist": list(SUT),
                 "dataset_and_manifest_window_match": normalized_window_same,
                 "same_window": query_meta_same and normalized_window_same,
                 "passed": query_meta_same and normalized_window_same and allowlist_match}
        return audit

    def audit_dataset_blindness(self, datasets: dict, run_id: str, fault_root: Path) -> dict[str, Any]:
        terms = {"ground_truth", "fault_id", "fault_class", "fault_type", "fault_target", "orchestrator_events", "workload_model",
                 "control_plane_dropped_flows", "flow_id", "validation_id", "confirmed_start", "confirmed_end"}
        scans = {}
        for scenario, dataset in datasets.items():
            paths = recursive_key_paths(dataset, terms)
            # Phase 5 datasets intentionally retain their operational run_id at the
            # root; Phase 6 is responsible for excluding it from model-visible input.
            without_operational_run_id = {key: value for key, value in dataset.items() if key != "run_id"}
            encoded = json.dumps(without_operational_run_id, sort_keys=True)
            literal_matches = {term: encoded.count(term) for term in (f"fault-{run_id}", self.validation_id)}
            scans[scenario] = {"keys": paths, "literal_control_values": literal_matches,
                               "operational_run_id": dataset.get("run_id"),
                               "dataset_root_has_only_operational_run_and_scenario": dataset.get("run_id") == run_id and
                                                                                   dataset.get("scenario") in ("A", "B")}
        forbidden = any(any(paths for paths in scan["keys"].values()) or any(scan["literal_control_values"].values()) for scan in scans.values())
        result = {"searched_terms": sorted(terms), "files_searched": ["scenario-a/dataset.json", "scenario-b/dataset.json"],
                  "scenarios": scans, "matches": sum(len(paths) for scan in scans.values() for paths in scan["keys"].values()),
                  "passed": not forbidden}
        return result

    def audit_requests(self, run_id: str, fault_root: Path, datasets: dict, dataset_dir: Path,
                       request_root: Path, request_a: dict, request_b: dict, manifests: dict) -> dict[str, Any]:
        requests = {"A": request_a, "B": request_b}
        counts = {s: request_counts(datasets[s], requests[s]) for s in ("A", "B")}
        ids = {s: evidence_comparison(datasets[s], requests[s]) for s in ("A", "B")}
        no_telemetry_a = {k: v for k, v in request_a.items() if k != "telemetry"}
        no_telemetry_b = {k: v for k, v in request_b.items() if k != "telemetry"}
        static_hash_a, static_hash_b = canonical_hash(no_telemetry_a), canonical_hash(no_telemetry_b)
        leak_terms = {"ground_truth", "fault_id", "fault_class", "fault_target", "validation_id", "orchestrator_events",
                      "workload_model", "control_plane_dropped_flows", "confirmed_start", "confirmed_end"}
        leakage: dict[str, Any] = {}
        treatment: dict[str, Any] = {}
        telemetry_integrity: dict[str, Any] = {}
        for scenario, request in requests.items():
            fields = recursive_key_paths(request, leak_terms)
            encoded = json.dumps(request, sort_keys=True, ensure_ascii=False)
            root_keys = set(request)
            explicit = marker_key_paths(request)
            leakage[scenario] = {
                "run_id_literal_matches": encoded.count(run_id),
                "validation_id_literal_matches": encoded.count(self.validation_id),
                "root_scenario_fields": [key for key in ("scenario", "run_id") if key in root_keys],
                "ground_truth_control_keys": fields,
                "fault_type_contract_only_paths": recursive_key_paths(request.get("output_contract", {}), {"fault_type"}).get("fault_type", []),
                "passed": encoded.count(run_id) == 0 and encoded.count(self.validation_id) == 0 and "scenario" not in root_keys
                          and not any(path for paths in fields.values() for path in paths) and not explicit,
            }
            treatment[scenario] = {"explicit_marker_paths": explicit,
                                   "removed_fields": manifests[scenario].get("model_visible_sanitization", {}).get("removed_fields", {}),
                                   "native_metadata_preserved": manifests[scenario].get("native_telemetry_provenance_markers", {}),
                                   "passed": not explicit and "model_visible_sanitization" in manifests[scenario]}
            expected_telemetry, expected_removed = expected_model_telemetry(datasets[scenario])
            manifest_removed = manifests[scenario].get("model_visible_sanitization", {}).get("removed_fields", {})
            marker_counts_equal = marker_counts_match(expected_removed, manifest_removed)
            telemetry_integrity[scenario] = {
                "matches_dataset_after_explicit_marker_removal": request.get("telemetry") == expected_telemetry,
                "source_marker_counts": expected_removed,
                "manifest_marker_counts": manifest_removed,
                "marker_counts_match": marker_counts_equal,
            }
        dataset_hashes = {s: sha256_file(dataset_dir / f"scenario-{s.lower()}/dataset.json") for s in ("A", "B")}
        request_hashes = {s: sha256_file(request_root / f"scenario-{s.lower()}/request.json") for s in ("A", "B")}
        preview_hashes = {s: sha256_file(request_root / f"scenario-{s.lower()}/request-preview.md") for s in ("A", "B")}
        audit = {
            "counts": counts, "evidence_ids": ids,
            "static_contract": {"scenario_a_sha256": static_hash_a, "scenario_b_sha256": static_hash_b,
                                "match": static_hash_a == static_hash_b},
            "leakage": leakage, "explicit_treatment_markers": treatment, "telemetry_integrity": telemetry_integrity,
            "hashes": {"dataset": dataset_hashes, "request": request_hashes, "preview": preview_hashes,
                       "dataset_manifest_expected": {s: manifests[s]["source_dataset_sha256"] for s in ("A", "B")},
                       "request_manifest_expected": {s: manifests[s]["request_sha256"] for s in ("A", "B")},
                       "preview_manifest_expected": {s: manifests[s]["preview_sha256"] for s in ("A", "B")},
                       "prompt_sha256": {s: manifests[s]["prompt_sha256"] for s in ("A", "B")},
                       "request_schema_sha256": {s: manifests[s]["request_schema_sha256"] for s in ("A", "B")},
                       "output_schema_sha256": {s: manifests[s]["output_schema_sha256"] for s in ("A", "B")},
                       "dataset_matches": all(dataset_hashes[s] == manifests[s]["source_dataset_sha256"] for s in ("A", "B")),
                       "request_matches": all(request_hashes[s] == manifests[s]["request_sha256"] for s in ("A", "B")),
                       "preview_matches": all(preview_hashes[s] == manifests[s]["preview_sha256"] for s in ("A", "B")),
                       "static_files_identical": len({manifests[s]["prompt_sha256"] for s in ("A", "B")}) == 1 and
                                                 len({manifests[s]["request_schema_sha256"] for s in ("A", "B")}) == 1 and
                                                 len({manifests[s]["output_schema_sha256"] for s in ("A", "B")}) == 1},
            "checks": {
                "request_counts_match": all(counts[s]["match"] for s in ("A", "B")),
                "request_evidence_ids_match": all(ids[s]["match"] for s in ("A", "B")),
                "static_contract_parity": static_hash_a == static_hash_b,
                "no_direct_control_leakage": all(leakage[s]["passed"] for s in ("A", "B")),
                "explicit_treatment_markers_zero": all(treatment[s]["passed"] for s in ("A", "B")),
                "telemetry_preserved_except_explicit_markers": all(item["matches_dataset_after_explicit_marker_removal"] and item["marker_counts_match"] for item in telemetry_integrity.values()),
                "hash_provenance": all(dataset_hashes[s] == manifests[s]["source_dataset_sha256"] and
                                        request_hashes[s] == manifests[s]["request_sha256"] and
                                        preview_hashes[s] == manifests[s]["preview_sha256"] for s in ("A", "B")) and
                                   all(len({manifests[s][field] for s in ("A", "B")}) == 1 for field in
                                       ("prompt_sha256", "request_schema_sha256", "output_schema_sha256")),
            }
        }
        return audit

    def dataset_signal_summary(self, dataset_dir: Path, scenario: str, stats: dict) -> dict[str, Any]:
        scenario_dir = dataset_dir / f"scenario-{scenario.lower()}"
        dataset_path = scenario_dir / "dataset.json"
        result = {"dataset": self.artifact(dataset_path), "stats": self.artifact(scenario_dir / "stats.json"),
                  "metrics": stats.get("metrics", {}), "logs": stats.get("logs", {}), "traces": stats.get("traces", {}),
                  "services": stats.get("services", {})}
        for signal in ("metrics", "logs", "traces"):
            raw_path = scenario_dir / "raw" / f"{signal}.json"
            result.setdefault("raw", {})[signal] = self.artifact(raw_path)
        return result

    def validate_cleanup(self, fault: str, fault_root: Path, result: dict) -> None:
        current = self.capture_cluster_snapshot(fault_root / "audit/cleanup", f"{fault.lower()}-cleanup")
        chaos = {key: len(current.get(key, {}).get("items", [])) for key in ("stresschaos", "networkchaos")}
        deploy = {(item.get("metadata", {}).get("namespace"), item.get("metadata", {}).get("name")): item for item in current.get("deployments", {}).get("items", [])}
        ready = all(int(deploy.get(("default", name), {}).get("status", {}).get("readyReplicas", 0)) == int(deploy.get(("default", name), {}).get("spec", {}).get("replicas", 0)) for name in SUT)
        f4_flag_ok = True
        if fault == "F4":
            pods = [p for p in current.get("pods", {}).get("items", []) if p.get("metadata", {}).get("namespace") == "default" and p.get("metadata", {}).get("labels", {}).get("app") == "paymentservice"]
            f4_flag_ok = bool(pods)
            for pod in pods:
                check = self.recorder.command(["kubectl", "exec", "-n", "default", pod["metadata"]["name"], "-c", "server", "--", "sh", "-c", "test ! -e /tmp/tcc-faults/payment-charge-exception"], stage="f4-cleanup-flag", fault=fault, check=False)
                f4_flag_ok &= check.returncode == 0
        passed = chaos == {"stresschaos": 0, "networkchaos": 0} and ready and f4_flag_ok
        result["statuses"]["cleanup"] = "PASS" if passed else "FAIL"
        result["cleanup"] = {"residual_chaos": chaos, "all_sut_deployments_ready": ready, "f4_flag_absent": f4_flag_ok,
                              "snapshot": self.recorder.relative(fault_root / "audit/cleanup/snapshot-summary.json"), "passed": passed}
        if not passed:
            result["failures"].append({"stage": "cleanup_failed", "reason": "residual chaos, non-ready SUT or F4 flag remains"})

    def state_comparison(self, fault: str, fault_root: Path) -> dict[str, Any]:
        states = {}
        for phase in ("before", "injection", "recovery"):
            base = fault_root / "audit" / phase
            if fault in ("F3", "F5"):
                deployment = json_load_optional(base / "deployment.json", {})
                endpoints = json_load_optional(base / "endpoints.json", {})
                states[phase] = {"desired_replicas": deployment.get("spec", {}).get("replicas"),
                                 "ready_replicas": deployment.get("status", {}).get("readyReplicas", 0),
                                 "endpoint_addresses": endpoint_addresses(endpoints)}
            elif fault == "F4":
                deployment = json_load_optional(base / "deployment.json", {})
                pods = json_load_optional(base / "pods.json", {}).get("items", [])
                flags = {p.stem.removeprefix("flag-"): p.read_text(encoding="utf-8").strip()
                         for p in sorted(base.glob("flag-*.txt"))}
                states[phase] = {"desired_replicas": deployment.get("spec", {}).get("replicas"),
                                 "ready_replicas": deployment.get("status", {}).get("readyReplicas", 0),
                                 "flags": flags,
                                 "pods": {pod.get("metadata", {}).get("uid"): restart_count_pod(pod) for pod in pods}}
            else:
                deployment = json_load_optional(base / "deployment.json", {})
                pods = json_load_optional(base / "pods.json", {}).get("items", [])
                state = {"deployment": "checkoutservice" if fault == "F1" else "paymentservice",
                         "desired_replicas": deployment.get("spec", {}).get("replicas"),
                         "ready_replicas": deployment.get("status", {}).get("readyReplicas", 0),
                         "pod_restarts": {pod.get("metadata", {}).get("uid"): restart_count_pod(pod) for pod in pods}}
                if phase == "injection":
                    chaos_kind = "stresschaos" if fault == "F1" else "networkchaos"
                    chaos = json_load_optional(base / "confirmed-chaos-resource.json", {})
                    if not chaos:
                        chaos = json_load_optional(base / f"{chaos_kind}.json", {})
                    state["chaos_resource"] = chaos
                    state["source_deployment"] = json_load_optional(base / "source-deployment.json", {}) if fault == "F2" else None
                states[phase] = state
        result = {"fault": fault, "states": states,
                  "evidence": {phase: self.recorder.relative(fault_root / "audit" / phase) for phase in states}}
        write_json(fault_root / "audit/state-comparison.json", result)
        return result

    def safe_to_continue(self, fault: str) -> bool:
        result = self.capture_cluster_snapshot(self.root / fault.lower() / "audit/continue-safety", f"{fault.lower()}-continue-safety")
        health = self.health_from_snapshot(result)
        return health["passed"] and health["residual_chaos"] == {"stresschaos": 0, "networkchaos": 0} and health["f4_flags_absent"]

    def health_from_snapshot(self, snapshot: dict) -> dict[str, Any]:
        deps = {(item.get("metadata", {}).get("namespace"), item.get("metadata", {}).get("name")): item for item in snapshot.get("deployments", {}).get("items", [])}
        statuses = {}
        for name in SUT:
            item = deps.get(("default", name), {})
            desired, ready = int(item.get("spec", {}).get("replicas", 0)), int(item.get("status", {}).get("readyReplicas", 0))
            statuses[name] = {"desired": desired, "ready": ready, "passed": desired > 0 and ready == desired}
        obs_statuses = {}
        for namespace, names in OBS_DEPLOYMENTS.items():
            for name in names:
                item = deps.get((namespace, name), {})
                desired, ready = int(item.get("spec", {}).get("replicas", 0)), int(item.get("status", {}).get("readyReplicas", 0))
                obs_statuses[f"{namespace}/{name}"] = {"desired": desired, "ready": ready,
                                                        "passed": desired > 0 and ready == desired}
        promtail_ds = next((item for item in snapshot.get("daemonsets", {}).get("items", [])
                            if item.get("metadata", {}).get("namespace") == "observability-conventional"
                            and item.get("metadata", {}).get("name") == "promtail-conventional"), {})
        promtail = {"desired": int(promtail_ds.get("status", {}).get("desiredNumberScheduled", 0)),
                    "ready": int(promtail_ds.get("status", {}).get("numberReady", 0))}
        promtail["passed"] = promtail["desired"] > 0 and promtail["ready"] == promtail["desired"]
        residual = {kind: len(snapshot.get(kind, {}).get("items", [])) for kind in ("stresschaos", "networkchaos")}
        flags_absent = True
        pods = [p for p in snapshot.get("pods", {}).get("items", []) if p.get("metadata", {}).get("namespace") == "default" and p.get("metadata", {}).get("labels", {}).get("app") == "paymentservice"]
        for pod in pods:
            check = self.recorder.command(["kubectl", "exec", "-n", "default", pod["metadata"]["name"], "-c", "server", "--", "sh", "-c", "test ! -e /tmp/tcc-faults/payment-charge-exception"], stage="health-f4-flag", check=False)
            flags_absent &= check.returncode == 0
        passed = (all(item["passed"] for item in statuses.values()) and all(item["passed"] for item in obs_statuses.values())
                  and promtail["passed"] and residual == {"stresschaos": 0, "networkchaos": 0} and flags_absent)
        return {"deployments": statuses, "observability_deployments": obs_statuses, "promtail": promtail,
                "residual_chaos": residual, "f4_flags_absent": flags_absent, "passed": passed}

    def timeline(self, events: list[dict], fault_root: Path, run_dir: Path) -> dict[str, Any]:
        names = ("workload_started", "warmup_started", "warmup_ended", "steady_started", "fault_requested", "fault_confirmed",
                 "recovery_requested", "recovery_confirmed", "admission_stopped", "workload_ended", "workload_finished")
        timeline = {name: next((event.get("timestamp") for event in events if event.get("event") == name), None) for name in names}
        timeline["dataset_collection_started"] = None
        timeline["dataset_collection_finished"] = None
        timeline["rca_request_generated_started"] = None
        timeline["rca_request_generated"] = None
        return timeline

    def write_fault_result(self, fault_root: Path, value: dict) -> None:
        write_json(fault_root / "validation.json", value)

    def write_json_reference(self, path: Path, value: Any) -> None:
        write_json(path, value)

    def write_fault_summary(self, fault_root: Path, result: dict) -> None:
        lines = [f"# {result['fault']} — Integration Evidence Summary", "",
                 f"Run: `{result['run_id']}`", f"Overall: **{result['overall']}**", "",
                 "## Checks", "", "| Check | Result | Evidence |", "|---|---|---|"]
        for key, value in result.get("statuses", {}).items():
            lines.append(f"| {key} | {value} | `validation.json` and referenced audit artifacts |")
        lines += ["", "## Timeline", "", "| Event | UTC |", "|---|---|"]
        for key, value in result.get("timeline", {}).items(): lines.append(f"| {key} | {value or 'not observed'} |")
        lines += ["", "## Injection, recovery, and cleanup", "", "```json",
                  json.dumps({"injection": result.get("injection", {}), "cleanup": result.get("cleanup", {})}, indent=2), "```", "",
                  "## Dataset, request, and provenance", "", "```json",
                  json.dumps({"dataset": result.get("dataset", {}), "request": result.get("request", {}),
                              "provenance": result.get("provenance", {})}, indent=2), "```", ""]
        lines += ["", "## Workload", "", "```json", json.dumps(result.get("workload", {}), indent=2), "```", "",
                  "## Warnings", ""]
        lines += [f"- {item}" for item in result.get("warnings", [])] or ["- None recorded."]
        lines += ["", "## Failures", ""]
        lines += [f"- `{item.get('stage')}`: {item.get('reason')}" for item in result.get("failures", [])] or ["- None recorded."]
        lines += ["", "## Evidence references", "", "Full command output and snapshots are indexed in `../evidence-index.md` and `validation.json`.", ""]
        (fault_root / "evidence-summary.md").write_text("\n".join(lines), encoding="utf-8")

    def artifact(self, path: Path) -> dict[str, Any]:
        if not path.is_file(): return {"path": self.recorder.relative(path), "exists": False}
        return {"path": self.recorder.relative(path), "sha256": sha256_file(path), "bytes": path.stat().st_size, "exists": True}

    def make_combined_log(self, stdout: Path, stderr: Path, destination: Path) -> None:
        destination.write_text("=== stdout ===\n" + stdout.read_text(errors="replace") + "\n=== stderr ===\n" + stderr.read_text(errors="replace"), encoding="utf-8")

    def finalize(self, final_snapshot: dict) -> None:
        self.campaign["finished_at"] = utc_now()
        self.campaign["faults_completed"] = len(self.campaign["faults"])
        self.campaign["valid_runs"] = sum(1 for item in self.campaign["faults"].values() if item.get("overall") == "PASS")
        self.campaign["warning_count"] = len(self.campaign["warnings"])
        self.campaign["failure_count"] = len(self.campaign["failures"])
        global_ok = self.campaign.get("global", {}).get("preflight", {}).get("passed", False) and self.campaign.get("global", {}).get("final_health", {}).get("passed", False) and self.campaign.get("global", {}).get("final_observability", {}).get("passed", False)
        self.campaign["overall"] = "PASS" if global_ok and self.campaign["valid_runs"] == len(self.faults) else "FAIL"
        self.campaign["classification"] = "integration_validation_not_for_scientific_inference"
        write_json(self.root / "campaign-summary.json", self.campaign)
        self.write_validation_report()
        self.write_evidence_index()

    def write_validation_report(self) -> None:
        lines = ["# Fase 6.5 — Validation Report", "",
                 "> Esta execução valida engenharia e integração. Seus resultados não fazem parte da campanha oficial e não devem ser usados para inferência científica sobre A/B.", "",
                 "## Executive summary", "", f"- Validation ID: `{self.validation_id}`", f"- Overall: **{self.campaign.get('overall', 'FAIL')}**",
                 f"- Valid runs: {self.campaign.get('valid_runs', 0)}/{len(self.faults)}", "",
                 "| Fault | GT | Workload/run | Injection | Recovery | Obs | Dataset | RCA Request | Provenance | Cleanup | Overall |",
                 "|---|---|---|---|---|---|---|---|---|---|---|"]
        for fault in FAULTS:
            entry = self.campaign["faults"].get(fault, {})
            status = entry.get("statuses", {})
            lines.append("| " + fault + " | " + " | ".join(status.get(key, "NOT RUN") for key in
                          ("ground_truth", "fault_run", "injection", "recovery", "observability", "dataset", "rca_request", "provenance", "cleanup")) + f" | {entry.get('overall', 'NOT RUN')} |")
        lines += ["", "## Environment and preflight", "", f"Environment snapshot: `{self.recorder.relative(self.root / 'environment.json')}`", "",
                  "Preflight evidence is saved under `global/preflight/` and initial cluster snapshots under `global/initial-state/`.", "",
                  "## Fault runs", ""]
        for fault in self.faults:
            entry = self.campaign["faults"].get(fault)
            if not entry:
                lines += [f"### {fault}", "", "Not run (campaign aborted before this stage).", ""]
                continue
            lines += [f"### {fault} — `{entry.get('run_id')}`", "", f"Overall: **{entry.get('overall', 'FAIL')}**", "",
                      f"Configuration: `{entry.get('config_path')}`; SHA-256 `{entry.get('config_sha256')}`.", "",
                      "| Claim | Observed values | Evidence |", "|---|---|---|"]
            gt = entry.get("ground_truth", {})
            lines.append(f"| Ground Truth timing | confirmed `{gt.get('timing', {}).get('confirmed_start')}` → `{gt.get('timing', {}).get('confirmed_end')}` | `{entry.get('evidence', {}).get('ground_truth', {}).get('path')}` |")
            workload = entry.get("workload", {}).get("values", {})
            lines.append(f"| Workload | scheduled {workload.get('scheduled_flows')}, started {workload.get('started_flows')}, completed {workload.get('completed_flows')}, failed {workload.get('failed_flows')}, drops {workload.get('control_plane_dropped_flows')}, observed start rate {workload.get('observed_flow_start_rate_per_second')} flows/s | `{entry.get('evidence', {}).get('run_metadata', {}).get('path')}` |")
            window = entry.get("dataset", {}).get("window_chain", {})
            lines.append(f"| GT → Dataset window | expected `{window.get('expected_dataset_window')}`; chain match `{window.get('match')}` | `{fault.lower()}/audit/dataset/window-chain.json` |")
            req = entry.get("request", {})
            lines.append(f"| Dataset → Request | counts match `{req.get('checks', {}).get('request_counts_match')}`; evidence IDs match `{req.get('checks', {}).get('request_evidence_ids_match')}`; A/B static hashes `{req.get('static_contract', {}).get('scenario_a_sha256')}` / `{req.get('static_contract', {}).get('scenario_b_sha256')} | `{fault.lower()}/audit/rca-request/audit.json` |")
            lines += ["", f"Evidence summary: `{fault.lower()}/evidence-summary.md`.", "", "Before/during/after snapshots and raw command output are in the fault audit directory and linked from `evidence-index.md`.", ""]
        lines += ["## Cross-run invariants", "", "- A and B static request parity is verified using canonical JSON hashes per run.",
                  "- Run ID, validation ID, and the dataset root scenario field are checked against the model-visible request.",
                  "- Explicit `telemetry_scenario` markers are checked in requests and sanitizer counts are retained in RCA manifests.",
                  "- Native SDK/scope/resource metadata remains in requests and is not treated as a leakage failure.",
                  "- Evidence IDs, counts, request/dataset hashes, equal query windows, and recovery state are recorded per run.", "",
                  "## Final state", "", "See `global/final-state/snapshot-summary.json`, its JSON command captures, and `global/final-state/observability/result.json`.", "",
                  "## Warnings and anomalies", ""]
        lines += [f"- {warning}" for warning in self.campaign["warnings"]] or ["- None recorded."]
        lines += ["", "## Failures", ""]
        lines += [f"- {failure}" for failure in self.campaign["failures"]] or ["- None recorded."]
        lines += ["", "## Manual review checklist", "",
                  "- [ ] A fault was applied and its target was evidenced.", "- [ ] Recovery is evidenced by the SUT's final state.",
                  "- [ ] A and B used the same query window.", "- [ ] Dataset and request counts/evidence IDs match.",
                  "- [ ] No Ground Truth answer appears in model-visible input.", "- [ ] Explicit treatment labels were sanitized and counted.",
                  "- [ ] Native telemetry metadata was preserved.", "- [ ] Final cluster state is healthy.", "- [ ] A human reviewed the evidence (not auto-marked).", ""]
        (self.root / "validation-report.md").write_text("\n".join(lines), encoding="utf-8")

    def write_evidence_index(self) -> None:
        lines = ["# Evidence Index", "", f"Validation: `{self.validation_id}`", "",
                 "All paths are repository-relative. The table stores the current hash and size of each file.", ""]
        for fault in FAULTS:
            root = self.root / fault.lower()
            lines += [f"## {fault}", ""]
            if not root.exists():
                lines += ["Not run.", ""]
                continue
            lines += ["### Injection and recovery", "", f"- Before snapshots: `{fault.lower()}/audit/before/`",
                      f"- Injection confirmation snapshot: `{fault.lower()}/audit/injection/`",
                      f"- Recovery snapshot: `{fault.lower()}/audit/recovery/`",
                      f"- After/cleanup snapshots: `{fault.lower()}/audit/after/`, `{fault.lower()}/audit/cleanup/`",
                      f"- Orchestrator events: `{fault.lower()}/orchestrator-events.jsonl`",
                      f"- Ground Truth: `artifacts/experiments/{run_id_for(self.validation_id, fault)}/ground-truth.json`", "",
                      "### Telemetry and model inputs", "",
                      f"- Dataset manifest and raw queries: `artifacts/datasets/{run_id_for(self.validation_id, fault)}/manifest.json`",
                      f"- Dataset A/B: `artifacts/datasets/{run_id_for(self.validation_id, fault)}/scenario-a|b/dataset.json`",
                      f"- RCA request A/B: `artifacts/rca-inputs/{run_id_for(self.validation_id, fault)}/scenario-a|b/request.json`",
                      f"- Machine validation: `{fault.lower()}/validation.json`", ""]
        lines += ["## Artifact integrity", "", "| Artifact | Path | SHA-256 | Bytes |", "|---|---|---|---:|"]
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and path.name not in ("evidence-index.md", "validation-report.md", "campaign-summary.json"):
                artifact = self.artifact(path)
                lines.append(f"| {path.name} | `{artifact['path']}` | `{artifact.get('sha256', 'missing')}` | {artifact.get('bytes', 0)} |")
        (self.root / "evidence-index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def restart_count_pod(pod: dict) -> int:
    return sum(int(item.get("restartCount", 0)) for item in pod.get("status", {}).get("containerStatuses", []))


def restart_counts(pods: list[dict]) -> dict[str, int]:
    return {f"{item.get('metadata', {}).get('namespace')}/{item.get('metadata', {}).get('name')}": restart_count_pod(item) for item in pods}


def endpoint_addresses(document: dict) -> int:
    return sum(len(subset.get("addresses", [])) for subset in document.get("subsets", []))


def json_load_optional(path: Path, default: Any) -> Any:
    try: return read_json(path)
    except (OSError, json.JSONDecodeError): return default


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file(): return []
    result = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try: result.append(json.loads(line))
        except json.JSONDecodeError: continue
    return result


def extract_json_document(text: str) -> dict | None:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char == "{":
            try:
                value, _ = decoder.raw_decode(text[index:])
                if isinstance(value, dict) and "status" in value:
                    return value
            except json.JSONDecodeError:
                continue
    return None


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Evidence-first integrated validation for the TCC experiment pipeline")
    parser.add_argument("--validation-id", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--all", action="store_true")
    mode.add_argument("--fault", choices=tuple(f.lower() for f in FAULTS))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--pre-buffer", type=float, default=float(os.environ.get("TCC_DATASET_PRE_BUFFER_SECONDS", "10")))
    parser.add_argument("--post-buffer", type=float, default=float(os.environ.get("TCC_DATASET_POST_BUFFER_SECONDS", "10")))
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        validation_id = validate_validation_id(args.validation_id)
        faults = select_faults(args.all, args.fault)
        if args.pre_buffer < 0 or args.post_buffer < 0:
            raise ValidationError("dataset buffers must be non-negative")
        if args.dry_run:
            print(json.dumps({"validation_id": validation_id, "fault_order": faults,
                              "run_ids": {fault: run_id_for(validation_id, fault) for fault in faults},
                              "configs": {fault: str(FAULT_CONFIGS[fault].relative_to(REPO)) for fault in faults},
                              "pre_buffer": args.pre_buffer, "post_buffer": args.post_buffer,
                              "commands": {fault: [
                                  ["scripts/experiment/run-experiment.sh", "--config", str(FAULT_CONFIGS[fault].relative_to(REPO)),
                                   "--run-id", run_id_for(validation_id, fault), "--confirmation-timeout", "90"],
                                  ["scripts/experiment/validate-observability.sh"],
                                  ["scripts/experiment/build-dataset.sh", "build", "--run-id", run_id_for(validation_id, fault),
                                   "--pre-buffer", str(args.pre_buffer), "--post-buffer", str(args.post_buffer)],
                                  ["scripts/experiment/build-rca-input.sh", "--run-id", run_id_for(validation_id, fault)],
                              ] for fault in faults},
                              "expected_artifacts": {fault: {
                                  "audit": f"artifacts/integration-validation/{validation_id}/{fault.lower()}/",
                                  "experiment": f"artifacts/experiments/{run_id_for(validation_id, fault)}/",
                                  "dataset": f"artifacts/datasets/{run_id_for(validation_id, fault)}/scenario-a|b/",
                                  "rca_request": f"artifacts/rca-inputs/{run_id_for(validation_id, fault)}/scenario-a|b/",
                              } for fault in faults}}, indent=2))
            return 0
        validation = IntegrationValidation(validation_id, faults, args.pre_buffer, args.post_buffer)
        return validation.run()
    except Exception as exc:
        print(f"integration validation: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
