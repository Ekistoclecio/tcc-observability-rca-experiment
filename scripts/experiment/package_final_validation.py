from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

REPO = Path(__file__).resolve().parents[2]
RESULTS = REPO / "audit/results"
FINAL_AUDIT = REPO / "audit/final-validation"
TEST_ROOT = REPO / "teste"
FAULT_ORDER = (
    ("001_cpu_saturation_r01", "cpu_saturation"),
    ("002_network_latency_r01", "network_latency"),
    ("003_service_unavailable_r01", "service_unavailable"),
    ("004_application_exception_r01", "application_exception"),
    ("005_datastore_unavailable_r01", "datastore_unavailable"),
)
SUT_SERVICES = (
    "frontend", "checkoutservice", "cartservice", "productcatalogservice", "currencyservice",
    "paymentservice", "shippingservice", "emailservice", "recommendationservice", "adservice", "redis-cart",
)


class AuditPackageError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def info(path: Path) -> dict[str, Any]:
    return {"path": path.as_posix(), "bytes": path.stat().st_size, "sha256": sha256(path)}


def clean_exact(repo_root: Path, target: Path, child_name: str) -> Path:
    repo = repo_root.resolve()
    relative = Path(child_name)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise AuditPackageError(f"unsafe cleanup target declaration: {child_name!r}")
    expected = repo / relative
    cursor = repo
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise AuditPackageError(f"refusing to clean a path containing a symlink: {cursor}")
    if target.is_symlink():
        raise AuditPackageError(f"refusing to clean a symlink: {target}")
    resolved = target.resolve(strict=False)
    if resolved != expected or resolved == repo or not resolved.is_relative_to(repo):
        raise AuditPackageError(f"refusing cleanup outside the exact repository path {expected}: {target}")
    if resolved.exists():
        if not resolved.is_dir():
            raise AuditPackageError(f"cleanup target is not a directory: {resolved}")
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True)
    return resolved


def span_failed(span: dict[str, Any]) -> bool:
    status = str(span.get("status") or "").casefold()
    attrs = span.get("attributes", {})
    return status in {"error", "failed", "failure", "2"} or str(attrs.get("otel.status_code", "")).casefold() in {"error", "2"} or bool(attrs.get("error"))


def deterministic_sample(dataset: dict[str, Any], log_cap: int = 80) -> dict[str, Any]:
    metrics: list[dict[str, Any]] = []
    grouped_metrics: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in dataset.get("metrics", []):
        grouped_metrics[str(item.get("concept", "unknown"))].append(item)
    for concept in sorted(grouped_metrics):
        ordered = sorted(grouped_metrics[concept], key=lambda x: (
            str(x.get("service", "")), str(x.get("operation", "")), str(x.get("concept", "")),
            str(x.get("timestamp", "")), str(x.get("evidence_id", ""))))
        metrics.extend(ordered[:3])

    grouped_logs: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in dataset.get("logs", []):
        grouped_logs[(str(item.get("service", "")), str(item.get("severity", "UNKNOWN")))].append(item)
    selected_logs: dict[str, dict[str, Any]] = {}
    for key in sorted(grouped_logs):
        ordered = sorted(grouped_logs[key], key=lambda x: (str(x.get("timestamp", "")), str(x.get("evidence_id", ""))))
        chosen = ordered[:2] + ordered[-2:]
        for row in chosen:
            selected_logs[str(row.get("evidence_id", json.dumps(row, sort_keys=True)))] = row
    logs = [selected_logs[key] for key in sorted(selected_logs)][:log_cap]

    traces = sorted(dataset.get("traces", []), key=lambda x: (str(x.get("trace_id", "")), str(x.get("start", ""))))
    error_traces, success_traces = [], []
    for trace in traces:
        (error_traces if any(span_failed(span) for span in trace.get("spans", [])) else success_traces).append(trace)
    selected_traces = []
    if success_traces:
        selected_traces.append(success_traces[0])
    if error_traces:
        selected_traces.append(error_traces[0])
    selected_traces.sort(key=lambda x: (str(x.get("start", "")), str(x.get("trace_id", ""))))
    return {"schema_version": "1.0", "sampling_policy": {
        "metrics": "up to 3 per semantic concept after stable service/operation/concept/timestamp/evidence_id ordering",
        "logs": f"first 2 and last 2 per service+severity, stable ordering, global cap {log_cap}",
        "traces": "lexicographically smallest trace ID in each success/error class; all spans of selected traces",
        "ground_truth_blind": True,
    }, "metrics": metrics, "logs": logs, "traces": selected_traces}


def semantic_views(dataset: dict[str, Any]) -> dict[str, set[tuple[str, ...]]]:
    metrics = {
        (str(x.get("concept")), str(x.get("service")), str(x.get("operation")), str(x.get("kind")),
         str(x.get("outcome")), str(x.get("target"))) for x in dataset.get("metrics", [])
    }
    logs = {(str(x.get("service")), str(x.get("severity")), "application_event") for x in dataset.get("logs", [])}
    spans = {
        (str(span.get("service")), str(span.get("operation")), str(span.get("kind")),
         str(span.get("target")), "error" if span_failed(span) else "success")
        for trace in dataset.get("traces", []) for span in trace.get("spans", [])
    }
    return {"metrics": metrics, "logs": logs, "spans": spans}


def dataset_summary(dataset_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    dataset = read_json(dataset_path)
    metrics, logs, traces = dataset.get("metrics", []), dataset.get("logs", []), dataset.get("traces", [])
    counters = Counter(str(x.get("delta_status", "not_applicable")) for x in metrics if x.get("concept") in ("request_count", "request_duration"))
    summary = {
        "source": info(dataset_path), "run_id": dataset.get("run_id"), "scenario": dataset.get("scenario"),
        "window": dataset.get("window"), "metric_items": len(metrics),
        "metric_concepts": dict(sorted(Counter(str(x.get("concept")) for x in metrics).items())),
        "log_items": len(logs), "trace_items": len(traces),
        "span_items": sum(len(x.get("spans", [])) for x in traces),
        "services": sorted(set(dataset.get("services", [])) | {str(x.get("service")) for x in metrics + logs if x.get("service")}),
        "logs_with_trace_id": sum(bool(x.get("trace_id")) for x in logs),
        "logs_with_span_id": sum(bool(x.get("span_id")) for x in logs),
        "counter_statuses": dict(sorted(counters.items())),
        "missing_baseline_count": sum(x.get("delta_status") == "missing_baseline" for x in metrics),
    }
    return summary, dataset


def request_summary(request_path: Path, request_manifest_path: Path, dataset: dict[str, Any], run_id: str,
                    validation_id: str) -> dict[str, Any]:
    request = read_json(request_path)
    serialized = request_path.read_text(encoding="utf-8")
    manifest = read_json(request_manifest_path)
    telemetry = request.get("telemetry", {})
    def keys(value: Any) -> list[str]:
        if isinstance(value, dict):
            return [str(k) for k in value] + [key for child in value.values() for key in keys(child)]
        if isinstance(value, list):
            return [key for child in value for key in keys(child)]
        return []
    request_keys = keys(request)
    control_keys = keys({k: v for k, v in request.items() if k != "telemetry"})
    forbidden_operational_keys = {"run_id", "scenario", "ground_truth", "fault_id", "fault_class", "fault_target",
                                  "confirmed_start", "confirmed_end", "fault_location", "fault_parameters",
                                  "telemetry_scenario", "telemetry.scenario"}
    root_ids = {str(x.get("evidence_id")) for x in dataset.get("metrics", []) + dataset.get("logs", []) if x.get("evidence_id")}
    root_ids.update(str(trace.get("evidence_id")) for trace in dataset.get("traces", []) if trace.get("evidence_id"))
    root_ids.update(str(span.get("evidence_id")) for trace in dataset.get("traces", []) for span in trace.get("spans", []) if span.get("evidence_id"))
    request_ids = {str(x.get("evidence_id")) for x in telemetry.get("metrics", []) + telemetry.get("logs", []) if x.get("evidence_id")}
    request_ids.update(str(trace.get("evidence_id")) for trace in telemetry.get("traces", []) if trace.get("evidence_id"))
    request_ids.update(str(span.get("evidence_id")) for trace in telemetry.get("traces", []) for span in trace.get("spans", []) if span.get("evidence_id"))
    checks = {
        "run_id_literal_matches": serialized.count(run_id),
        "validation_id_literal_matches": serialized.count(validation_id),
        "explicit_treatment_marker_matches": sum(request_keys.count(k) for k in ("telemetry_scenario", "telemetry.scenario")),
        "operational_key_matches": sorted((Counter(control_keys) & Counter(forbidden_operational_keys)).elements()),
        "ground_truth_causal_fields_at_request_root": sorted(set(request) & {"fault", "location", "parameters", "confirmed_start", "confirmed_end"}),
    }
    return {
        "request_sha256": sha256(request_path), "request_bytes": request_path.stat().st_size,
        "prompt_sha256": manifest.get("prompt_sha256"), "schema_version": request.get("schema_version"),
        "metrics_count": len(telemetry.get("metrics", [])), "logs_count": len(telemetry.get("logs", [])),
        "traces_count": len(telemetry.get("traces", [])),
        "spans_count": sum(len(x.get("spans", [])) for x in telemetry.get("traces", [])),
        "sanitization": manifest.get("model_visible_sanitization"), "leakage_checks": checks,
        "dataset_count_match": {"metrics": len(dataset.get("metrics", [])) == len(telemetry.get("metrics", [])),
                                "logs": len(dataset.get("logs", [])) == len(telemetry.get("logs", [])),
                                "traces": len(dataset.get("traces", [])) == len(telemetry.get("traces", []))},
        "evidence_ids": {"dataset_count": len(root_ids), "request_count": len(request_ids),
                         "dataset_only": sorted(root_ids - request_ids), "request_only": sorted(request_ids - root_ids)},
    }


def implementation_paths() -> dict[str, str]:
    paths = [
        "scripts/experiment/integration_validation.py", "scripts/experiment/experiment_data_validation.py",
        "scripts/experiment/run_results_battery.py", "scripts/experiment/package_final_validation.py",
        "scripts/experiment/faults/base.py", "scripts/experiment/faults/chaos_mesh.py",
        "scripts/experiment/faults/kubernetes.py", "scripts/experiment/faults/application.py",
        "scripts/experiment/orchestrator/config.py", "scripts/experiment/orchestrator/events.py",
        "scripts/experiment/orchestrator/ground_truth.py", "scripts/experiment/orchestrator/kube.py",
        "scripts/experiment/orchestrator/preflight.py", "scripts/experiment/orchestrator/run_experiment.py",
        "scripts/experiment/ground-truth/ground-truth.schema.json",
        "scripts/experiment/dataset/core.py", "scripts/experiment/dataset/backends.py",
        "scripts/experiment/dataset/builder.py", "scripts/experiment/dataset/contract.py",
        "scripts/experiment/dataset/normalize.py", "scripts/experiment/dataset/dataset.schema.json",
        "scripts/experiment/rca/builder.py", "scripts/experiment/rca/prompt.txt",
        "scripts/experiment/rca/rca-request.schema.json", "scripts/experiment/rca/rca-output.schema.json",
        "scripts/experiment/observability-contract/README.md", "scripts/experiment/observability-contract/metric-contract.json",
        "scripts/experiment/observability-contract/log-contract.json", "scripts/experiment/observability-contract/trace-contract.json",
        "scripts/experiment/validate-observability.sh", "scripts/experiment/run-experiment.sh",
        "scripts/experiment/build-dataset.sh", "scripts/experiment/build-rca-input.sh",
        "scripts/experiment/test_integration_validation.py", "scripts/experiment/test_experiment_data_validation.py",
        "scripts/experiment/test_run_results_battery.py", "scripts/experiment/dataset/test_dataset.py",
        "scripts/experiment/rca/test_rca_input.py", "scripts/experiment/orchestrator/test_orchestrator.py",
        "scripts/experiment/orchestrator/test_preflight.py", "scripts/experiment/package_final_validation_test.py",
        "configs/experiment/faults/f1-checkout-cpu.json", "configs/experiment/faults/f2-checkout-payment-latency.json",
        "configs/experiment/faults/f3-payment-unavailable.json", "configs/experiment/faults/f4-payment-charge-exception.json",
        "configs/experiment/faults/f5-redis-unavailable.json",
        "deployments/observability/conventional/prometheus.yaml", "deployments/observability/otel/collector.yaml",
        "deployments/observability/otel/prometheus.yaml", "deployments/observability/conventional/promtail.yaml",
        "deployments/observability/otel/jaeger.yaml", "deployments/observability/otel/loki.yaml",
        "deployments/experiment/otel-existing.yaml", "deployments/experiment/payment-fault-hook.yaml",
        "scripts/experiment/observability-contract/README.md",
        "scripts/experiment/observability-contract/metric-contract.json",
        "scripts/experiment/observability-contract/log-contract.json",
        "scripts/experiment/observability-contract/trace-contract.json",
        "scripts/experiment/dataset/stats.schema.json",
    ]
    for service in ("frontend", "checkoutservice", "cartservice", "productcatalogservice", "currencyservice", "paymentservice",
                    "shippingservice", "emailservice", "recommendationservice", "adservice"):
        source = REPO / "src" / service
        if source.is_dir():
            paths.extend(path.relative_to(REPO).as_posix() for path in sorted(source.glob("*telemetry*")) if path.is_file())
    return {item: "core validation, orchestration, instrumentation, contract, or test source" for item in sorted(set(paths)) if (REPO / item).is_file()}


def copy_implementation(target: Path, validation_commit: str) -> list[dict[str, Any]]:
    files_root = target / "implementation/files"
    manifest = []
    for original, reason in implementation_paths().items():
        source = REPO / original
        dest = files_root / original
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        digest = sha256(source)
        if sha256(dest) != digest:
            raise AuditPackageError(f"implementation snapshot hash mismatch: {original}")
        manifest.append({"original_path": original, "copied_path": dest.relative_to(target).as_posix(), "sha256": digest, "reason": reason})
    write_json(target / "implementation/manifest.json", {"validation_commit": validation_commit, "files": manifest})
    return manifest


def versions() -> dict[str, Any]:
    commands = {
        "docker": ["docker", "version", "--format", "{{.Client.Version}}/{{.Server.Version}}"],
        "kubectl": ["kubectl", "version", "--client", "-o", "json"],
        "kind": ["kind", "version"],
        "python": [sys.executable, "--version"],
    }
    result: dict[str, Any] = {}
    for name, command in commands.items():
        try:
            proc = subprocess.run(command, cwd=REPO, capture_output=True, text=True, timeout=20, check=False)
            result[name] = {"command": command, "exit_code": proc.returncode, "stdout": proc.stdout.strip(), "stderr": proc.stderr.strip()}
        except (OSError, subprocess.TimeoutExpired) as exc:
            result[name] = {"command": command, "available": False, "error": str(exc)}
    try:
        proc = subprocess.run(["kubectl", "version", "-o", "json"], cwd=REPO, capture_output=True, text=True, timeout=20, check=True)
        value = json.loads(proc.stdout)
        result["kubernetes"] = {"client": value.get("clientVersion", {}).get("gitVersion"), "server": value.get("serverVersion", {}).get("gitVersion")}
        result["online_boutique"] = "v0.10.7"
        chaos = subprocess.run(["kubectl", "get", "deployment", "chaos-controller-manager", "-n", "chaos-mesh", "-o", "jsonpath={.spec.template.spec.containers[0].image}"], cwd=REPO, capture_output=True, text=True, timeout=20, check=False)
        result["chaos_mesh_image"] = chaos.stdout.strip()
        collector = subprocess.run(["kubectl", "get", "deployment", "otel-collector", "-n", "observability-otel", "-o", "jsonpath={.spec.template.spec.containers[0].image}"], cwd=REPO, capture_output=True, text=True, timeout=20, check=False)
        result["otel_collector_image"] = collector.stdout.strip()
    except Exception as exc:
        result["cluster_versions_error"] = str(exc)
    return result


def raw_inventory(results_root: Path) -> dict[str, Any]:
    manifest = read_json(results_root / "manifest.json")
    inventory: dict[str, Any] = {"validation_id": manifest.get("validation_id"), "window": {
        "start": manifest.get("experiment_start"), "end": manifest.get("experiment_end")}, "scenarios": {}}
    for scenario in ("A", "B"):
        folder = results_root / f"dataset{scenario}"
        metrics, logs, traces = (read_json(folder / f"{name}.json") for name in ("metrics", "logs", "traces"))
        metric_rows = metrics.get("response", {}).get("data", {}).get("result", [])
        log_rows = logs.get("response", {}).get("data", {}).get("result", [])
        trace_rows = [trace for result in traces.get("search_results", []) for trace in result.get("response", {}).get("data", [])]
        trace_map = {trace.get("traceID"): trace for trace in trace_rows if trace.get("traceID")}
        services = sorted({str(row.get("metric", {}).get("service_name") or row.get("metric", {}).get("service.name") or row.get("metric", {}).get("service")) for row in metric_rows if row.get("metric", {}).get("service_name") or row.get("metric", {}).get("service.name") or row.get("metric", {}).get("service")})
        log_services = sorted({str(stream.get("stream", {}).get("service_name") or stream.get("stream", {}).get("service.name") or stream.get("stream", {}).get("service")) for stream in log_rows})
        severity = Counter(str(stream.get("stream", {}).get("severity") or stream.get("stream", {}).get("level") or "UNKNOWN") for stream in log_rows for _ in stream.get("values", []))
        inventory["scenarios"][scenario] = {
            "metrics": {**info(folder / "metrics.json"), "series": len(metric_rows), "samples": sum(len(row.get("values", [])) for row in metric_rows), "names": sorted({row.get("metric", {}).get("__name__") for row in metric_rows if row.get("metric", {}).get("__name__")}), "services": services},
            "logs": {**info(folder / "logs.json"), "records": sum(len(row.get("values", [])) for row in log_rows), "services": log_services, "severity_distribution_by_stream_labels": dict(sorted(severity.items()))},
            "traces": {**info(folder / "traces.json"), "traces": len(trace_map), "spans": sum(len(t.get("spans", [])) for t in trace_map.values()), "services": sorted({p.get("serviceName") for t in trace_map.values() for p in t.get("processes", {}).values() if p.get("serviceName")}), "operations": sorted({s.get("operationName") for t in trace_map.values() for s in t.get("spans", []) if s.get("operationName")})},
        }
    return inventory


def fault_sources(results_root: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for folder, fault_type in FAULT_ORDER:
        fault_dir = results_root / folder
        if not fault_dir.is_dir():
            raise AuditPackageError(f"missing fault result folder: {fault_dir}")
        gt = read_json(fault_dir / "ground-truth.json")
        a = read_json(fault_dir / "datasetA.json")
        b = read_json(fault_dir / "datasetB.json")
        if len({gt.get("run_id"), a.get("run_id"), b.get("run_id")}) != 1:
            raise AuditPackageError(f"A/B/GT run identity mismatch in {folder}")
        if gt.get("status") != "valid" or not gt.get("validation", {}).get("injection_confirmed") or not gt.get("validation", {}).get("recovery_confirmed"):
            raise AuditPackageError(f"invalid or unrecovered Ground Truth in {folder}")
        item = next((x for x in manifest.get("fault_runs", []) if x.get("directory") == folder), None)
        if not item:
            raise AuditPackageError(f"global results manifest does not list {folder}")
        rows.append({"directory": folder, "fault_type": fault_type, "run_id": gt["run_id"], "source": fault_dir,
                     "gt": gt, "A": a, "B": b, "result_manifest_entry": item})
    return rows


def make_fault_package(target: Path, row: dict[str, Any], validation_id: str, request_root: Path) -> dict[str, Any]:
    fault_dir = target / "faults" / row["directory"]
    fault_dir.mkdir(parents=True, exist_ok=False)
    gt_path = row["source"] / "ground-truth.json"
    shutil.copy2(gt_path, fault_dir / "ground-truth.json")
    summaries, views = {}, {}
    request_summaries = {}
    for label in ("A", "B"):
        dataset_path = row["source"] / f"dataset{label}.json"
        summary, dataset = dataset_summary(dataset_path)
        summaries[label], views[label] = summary, semantic_views(dataset)
        write_json(fault_dir / f"dataset{label}-summary.json", summary)
        write_json(fault_dir / f"dataset{label}-sample.json", deterministic_sample(dataset))
        request_dir = request_root / row["run_id"] / f"scenario-{label.lower()}"
        request_path, request_manifest_path = request_dir / "request.json", request_dir / "manifest.json"
        req_summary = request_summary(request_path, request_manifest_path, dataset, row["run_id"], validation_id)
        req_summary["source_request_manifest_sha256"] = sha256(request_manifest_path)
        request_summaries[label] = req_summary
        write_json(fault_dir / f"rca-input{label}-summary.json", req_summary)
    diffs = {signal: {"A_count": len(views["A"][signal]), "B_count": len(views["B"][signal]),
                      "A_only": [list(x) for x in sorted(views["A"][signal] - views["B"][signal])],
                      "B_only": [list(x) for x in sorted(views["B"][signal] - views["A"][signal])]}
             for signal in ("metrics", "logs", "spans")}
    same_window = summaries["A"].get("window") == summaries["B"].get("window")
    same_run = summaries["A"].get("run_id") == summaries["B"].get("run_id") == row["gt"].get("run_id")
    checks = {
        "ground_truth_valid": row["gt"].get("status") == "valid",
        "injection_and_recovery_confirmed": row["gt"].get("validation", {}).get("injection_confirmed") and row["gt"].get("validation", {}).get("recovery_confirmed"),
        "same_physical_run": same_run, "same_rca_window": same_window,
        "metric_semantic_parity": not diffs["metrics"]["A_only"] and not diffs["metrics"]["B_only"],
        "log_service_severity_parity": not diffs["logs"]["A_only"] and not diffs["logs"]["B_only"],
        "logical_span_parity": not diffs["spans"]["A_only"] and not diffs["spans"]["B_only"],
        "requests_valid": all(all(req["dataset_count_match"].values()) for req in request_summaries.values()),
        "no_run_or_validation_id_leakage": all(req["leakage_checks"]["run_id_literal_matches"] == 0 and req["leakage_checks"]["validation_id_literal_matches"] == 0 and not req["leakage_checks"]["operational_key_matches"] for req in request_summaries.values()),
        "no_explicit_treatment_markers": all(req["leakage_checks"]["explicit_treatment_marker_matches"] == 0 for req in request_summaries.values()),
        "evidence_ids_preserved": all(not req["evidence_ids"]["dataset_only"] and not req["evidence_ids"]["request_only"] for req in request_summaries.values()),
    }
    validation = {"fault_type": row["fault_type"], "run_id": row["run_id"], "checks": checks, "parity": diffs,
                  "collection_lag_observations": [], "dataset_counts": {x: {k: summaries[x][k] for k in ("metric_items", "log_items", "trace_items", "span_items")} for x in ("A", "B")},
                  "request_counts": {x: {k: request_summaries[x][f"{k}_count"] for k in ("metrics", "logs", "traces", "spans")} for x in ("A", "B")},
                  "ground_truth_sha256": sha256(fault_dir / "ground-truth.json"), "overall": "PASS" if all(checks.values()) else "FAIL"}
    write_json(fault_dir / "validation.json", validation)
    return {"directory": row["directory"], "run_id": row["run_id"], "fault_type": row["fault_type"], "overall": validation["overall"], "checks": checks,
            "ground_truth": info(fault_dir / "ground-truth.json"), "datasets": {x: {"summary": info(fault_dir / f"dataset{x}-summary.json"), "sample": info(fault_dir / f"dataset{x}-sample.json")} for x in ("A", "B")},
            "requests": {x: info(fault_dir / f"rca-input{x}-summary.json") for x in ("A", "B")}}


def prepare(validation_id: str) -> None:
    clean_exact(REPO, TEST_ROOT, "teste")
    clean_exact(REPO, FINAL_AUDIT, "audit/final-validation")
    write_json(FINAL_AUDIT / "validation-id.json", {"validation_id": validation_id, "prepared_at": datetime.now(timezone.utc).isoformat()})


def build(validation_id: str) -> dict[str, Any]:
    results_manifest = read_json(RESULTS / "manifest.json")
    if results_manifest.get("validation_id") != validation_id or results_manifest.get("status") != "complete":
        raise AuditPackageError("audit/results manifest is not complete for this validation ID")
    rows = fault_sources(RESULTS, results_manifest)
    root = TEST_ROOT
    if not root.is_dir() or any(root.iterdir()):
        raise AuditPackageError("teste/ must exist and be empty; run --prepare before the battery")
    validation_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=REPO, text=True).strip()
    if dirty:
        raise AuditPackageError("working tree changed since validation commit; refusing to create an audit package")
    (root / "environment").mkdir()
    (root / "contract").mkdir()
    (root / "validation").mkdir()
    write_json(root / "environment/versions.json", versions())
    (root / "environment/git.txt").write_text(f"commit={validation_commit}\nbranch={subprocess.check_output(['git','branch','--show-current'],cwd=REPO,text=True).strip()}\nstatus=clean\n", encoding="utf-8")
    global_window = {"experiment_start": results_manifest.get("experiment_start"), "experiment_end": results_manifest.get("experiment_end"),
                     "faults": [{"run_id": x["run_id"], "fault_type": x["fault_type"],
                                 "confirmed_start": x["gt"]["timing"]["confirmed_start"], "confirmed_end": x["gt"]["timing"]["confirmed_end"]} for x in rows]}
    write_json(root / "environment/validation-window.json", global_window)
    (root / "README.md").write_text(readme_text(validation_id, validation_commit), encoding="utf-8")

    contract_files = {"observability-contract.md": "scripts/experiment/observability-contract/README.md",
                      "metric-contract.json": "scripts/experiment/observability-contract/metric-contract.json",
                      "log-contract.json": "scripts/experiment/observability-contract/log-contract.json",
                      "trace-contract.json": "scripts/experiment/observability-contract/trace-contract.json"}
    for name, relative in contract_files.items():
        source = REPO / relative
        dest = root / "contract" / name
        shutil.copy2(source, dest)

    implementation_manifest = copy_implementation(root, validation_commit)
    raw = raw_inventory(RESULTS)
    write_json(root / "validation/raw-inventory.json", raw)
    fault_results = [make_fault_package(root, row, validation_id, REPO / "artifacts/rca-inputs") for row in rows]
    parity = {"validation_id": validation_id, "faults": []}
    for row in rows:
        validation = read_json(root / "faults" / row["directory"] / "validation.json")
        parity["faults"].append({"fault": row["directory"], "checks": validation["checks"], "differences": validation["parity"]})
    write_json(root / "validation/parity-summary.json", parity)

    leakage_rows = []
    for row in rows:
        for scenario in ("A", "B"):
            request_path = REPO / "artifacts/rca-inputs" / row["run_id"] / f"scenario-{scenario.lower()}" / "request.json"
            text = request_path.read_text(encoding="utf-8")
            leakage_rows.append({"run_id": row["run_id"], "request_sha256": sha256(request_path),
                "scenario_operational_only": scenario, "checks": {
                    "run_id_literal": text.count(row["run_id"]), "validation_id_literal": text.count(validation_id),
                    "telemetry_scenario_key": text.count("telemetry_scenario"), "telemetry.scenario_key": text.count("telemetry.scenario"),
                    "top_level_scenario": "\"scenario\"" in text[:text.find("\"telemetry\"")] if "\"telemetry\"" in text else False,
                }})
    leakage = {"requests_checked": len(leakage_rows), "requests": leakage_rows,
               "result": "ZERO" if all(all(v == 0 for k, v in row["checks"].items() if k != "top_level_scenario") and not row["checks"]["top_level_scenario"] for row in leakage_rows) else "ERROR"}
    write_json(root / "validation/leakage-scan.json", leakage)

    test_command = [sys.executable, "-m", "unittest", "scripts.experiment.dataset.test_dataset", "scripts.experiment.orchestrator.test_orchestrator", "scripts.experiment.orchestrator.test_preflight", "scripts.experiment.rca.test_rca_input", "scripts.experiment.test_experiment_data_validation", "scripts.experiment.test_integration_validation", "scripts.experiment.test_run_results_battery", "scripts.experiment.package_final_validation_test"]
    test_output = subprocess.run(test_command, cwd=REPO, capture_output=True, text=True, check=False)
    test_log = test_output.stdout + test_output.stderr
    match = re.search(r"Ran (\d+) tests? in", test_log)
    test_count = int(match.group(1)) if match else None
    tests_text = ("command: python3 -m unittest <8 experiment test modules>\n"
                  f"tests_run: {test_count if test_count is not None else 'unknown'}\n"
                  f"exit_code: {test_output.returncode}\n"
                  + "\n".join(line for line in test_log.splitlines() if line.startswith(("Ran ", "OK", "FAILED", "ERROR"))) + "\n")
    (root / "validation/tests.txt").write_text(tests_text, encoding="utf-8")

    # The representative chain is fixed in advance to F4. Include complete processed
    # datasets and requests; never copy the full-run RAW captures into this folder.
    rep = next(x for x in rows if x["directory"].startswith("004_application_exception"))
    chain = root / "representative-chain" / rep["directory"]
    chain.mkdir(parents=True)
    for name in ("ground-truth.json",):
        shutil.copy2(rep["source"] / name, chain / name)
    for scenario in ("A", "B"):
        shutil.copy2(rep["source"] / f"dataset{scenario}.json", chain / f"dataset{scenario}.json")
        req_src = REPO / "artifacts/rca-inputs" / rep["run_id"] / f"scenario-{scenario.lower()}" / "request.json"
        shutil.copy2(req_src, chain / f"rca-input{scenario}.json")
    write_json(chain / "manifest.json", {"fault": rep["directory"], "run_id": rep["run_id"],
        "ground_truth_sha256": sha256(chain / "ground-truth.json"),
        "dataset_sha256": {s: sha256(chain / f"dataset{s}.json") for s in ("A", "B")},
        "request_sha256": {s: sha256(chain / f"rca-input{s}.json") for s in ("A", "B")},
        "full_run_raw_included": False})

    summary = {"validation_id": validation_id, "validation_commit": validation_commit, "overall_status": "PASS" if all(x["overall"] == "PASS" for x in fault_results) and leakage["result"] == "ZERO" and test_output.returncode == 0 else "FAIL",
        "faults_executed": [x["fault_type"] for x in fault_results], "tests": {"passed": test_count if test_output.returncode == 0 else None, "failed": 0 if test_output.returncode == 0 else None, "exit_code": test_output.returncode},
        "environment": read_json(root / "environment/versions.json"), "raw_inventory_summary": {s: {k: v for k, v in raw["scenarios"][s].items()} for s in ("A", "B")},
        "semantic_parity": parity, "leakage": leakage["result"], "ground_truth": {"valid": len(rows), "total": 5},
        "builder": "Common RCA Telemetry View v1; Prometheus query step 1s; shared metric temporal semantics; missing baselines remain null",
        "rca_inputs": {"valid": sum(2 for x in fault_results if x["checks"]["requests_valid"] and x["checks"]["no_run_or_validation_id_leakage"] and x["checks"]["no_explicit_treatment_markers"]), "total": 10},
        "known_limitations": ["Prometheus 5s scrapes and the OTel 5s metric reader/Collector pipeline can leave a bounded collection lag at a strict cutoff; the Builder uses a shared 1s evaluation step and records sampled timestamps."]}
    write_json(root / "audit-summary.json", summary)

    final_dir = FINAL_AUDIT
    final_dir.mkdir(parents=True, exist_ok=True)
    write_json(final_dir / "validation-summary.json", summary)
    write_json(final_dir / "parity-report.json", parity)
    write_json(final_dir / "environment.json", read_json(root / "environment/versions.json"))
    write_json(final_dir / "hash-manifest.json", {"audit_results_manifest": info(RESULTS / "manifest.json"),
        "raw_inventory": raw, "package_hashes": hash_tree(root, exclude={"validation/hashes.json"})})
    (final_dir / "validation-report.md").write_text(report_text(validation_id, summary, fault_results, global_window, leakage), encoding="utf-8")
    hashes = hash_tree(root, exclude={"validation/hashes.json"})
    write_json(root / "validation/hashes.json", {"validation_id": validation_id, "validation_commit": validation_commit,
        "files": hashes, "external_artifacts": external_hashes(RESULTS, rows, REPO / "artifacts/rca-inputs")})
    size = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
    if size > 25 * 1024 * 1024:
        # Preserve full F4 datasets, but replace full model requests only; complete
        # requests remain available at their independently hashed source paths.
        for scenario in ("A", "B"):
            request = chain / f"rca-input{scenario}.json"
            request.unlink()
            summary_path = root / "faults" / rep["directory"] / f"rca-input{scenario}-summary.json"
            shutil.copy2(summary_path, chain / f"rca-input{scenario}-summary.json")
        README = root / "README.md"
        README.write_text(README.read_text(encoding="utf-8") + "\nO pacote ultrapassaria 25 MiB com os RCA Inputs completos; a cadeia representativa mantém os datasets F4 completos e substitui os requests completos por summaries auditáveis. Requests completos referenciados por hash permanecem em artifacts/rca-inputs/.\n", encoding="utf-8")
        write_json(root / "validation/hashes.json", {"validation_id": validation_id, "validation_commit": validation_commit,
            "files": hash_tree(root, exclude={"validation/hashes.json"}), "external_artifacts": external_hashes(RESULTS, rows, REPO / "artifacts/rca-inputs")})
        size = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
    print(f"audit_package={root}\nsize_bytes={size}\nfiles={sum(p.is_file() for p in root.rglob('*'))}\ntest_exit_code={test_output.returncode}")
    return summary


def hash_tree(root: Path, exclude: set[str] | None = None) -> dict[str, Any]:
    excluded = exclude or set()
    return {path.relative_to(root).as_posix(): {"sha256": sha256(path), "bytes": path.stat().st_size}
            for path in sorted(root.rglob("*")) if path.is_file() and path.relative_to(root).as_posix() not in excluded}


def external_hashes(results_root: Path, rows: list[dict[str, Any]], request_root: Path) -> dict[str, Any]:
    paths = [results_root / "datasetA/metrics.json", results_root / "datasetA/logs.json", results_root / "datasetA/traces.json",
             results_root / "datasetB/metrics.json", results_root / "datasetB/logs.json", results_root / "datasetB/traces.json"]
    for row in rows:
        paths.extend([row["source"] / f"dataset{s}.json" for s in ("A", "B")])
        paths.append(row["source"] / "ground-truth.json")
        paths.extend([request_root / row["run_id"] / f"scenario-{s.lower()}" / "request.json" for s in ("A", "B")])
    return {p.relative_to(REPO).as_posix(): info(p) for p in paths}


def report_text(validation_id: str, summary: dict[str, Any], faults: list[dict[str, Any]], window: dict[str, Any], leakage: dict[str, Any]) -> str:
    lines = ["# Final Integrated Validation", "", f"- Validation ID: `{validation_id}`", f"- Commit: `{summary['validation_commit']}`", f"- Overall: **{summary['overall_status']}**", f"- Global window: `{window['experiment_start']}` → `{window['experiment_end']}`", "", "| Fault | Ground Truth | Metrics | Logs | Traces | RCA input | Overall |", "|---|---|---|---|---|---|---|"]
    for fault in faults:
        checks = fault["checks"]
        lines.append(f"| {fault['fault_type']} | {checks['ground_truth_valid']} | {checks['metric_semantic_parity']} | {checks['log_service_severity_parity']} | {checks['logical_span_parity']} | {checks['requests_valid']} | {fault['overall']} |")
    lines += ["", "## Collection lag", "", "Prometheus scrape interval A/B=5s; OpenTelemetry metric reader=5s. The Builder evaluates Prometheus range queries at a shared 1s step. Values at the cutoff remain observed values; no interpolation or trace-derived metric values are used.", "", "## Leakage", "", f"RCA requests checked: {leakage['requests_checked']}; result: **{leakage['result']}**.", "", "## RAW snapshot", "", "Full-run RAW hashes, bytes, samples, records, traces, spans and services are in `teste/validation/raw-inventory.json`; source RAW payloads are not copied into `teste/`.", "", "## Interpretation", "", "Engineering validation only; not part of the official scientific campaign. No LLM was called and no RCA evaluation was performed.", ""]
    return "\n".join(lines)


def readme_text(validation_id: str, commit: str) -> str:
    return f"""# Pacote de auditoria independente

Validation ID: `{validation_id}`
Commit: `{commit}`

- **Cenário A**: telemetria convencional independente por sinal, com correlação interna nos traces.
- **Cenário B**: OpenTelemetry com contexto distribuído e correlação explícita de logs com spans quando disponível.
- **Hipótese experimental**: comparar a representação, estrutura, padronização semântica e correlação mantendo observações diagnósticas comuns.
- **Ground Truth**: registro do control plane sobre falha e janela efetiva; não é evidência do RCA Input.
- **RCA Dataset**: visão semântica compactada da telemetria de uma run e janela.
- **RCA Input**: Canonical Full RCA Request A/B; não houve chamada de modelo.
- **RAW**: snapshots integrais dos backends usados para auditoria e reprocessamento.

Esta pasta é um pacote reduzido para auditoria externa. Os RAWs completos permanecem em `audit/results/` e são representados aqui por hashes, inventários, estatísticas e amostras determinísticas.

O pacote permite revisar desenho, implementação, Ground Truth, Builder, RCA Input, paridade semântica e leakage. A cadeia representativa completa foi predeterminada como F4. A amostragem usa ordenação estável e não consulta Ground Truth.

Limitação conhecida: Prometheus usa scrape de 5 s e o caminho B inclui exportação OTel periódica; pode haver atraso no valor disponível no corte. O Dataset Builder usa `step=1s` em ambos os cenários e conserva o valor realmente observado.

Esta validação é de engenharia, não integra a campanha científica oficial e não deve ser utilizada para inferência sobre a qualidade diagnóstica A/B.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare or build the compact final validation audit package")
    parser.add_argument("--validation-id", required=True)
    parser.add_argument("--prepare", action="store_true", help="safely clear and recreate only teste/ and audit/final-validation/")
    parser.add_argument("--build", action="store_true", help="build package after audit/results is complete")
    args = parser.parse_args(argv)
    if args.prepare == args.build:
        parser.error("select exactly one of --prepare or --build")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,25}", args.validation_id):
        parser.error("validation-id must be lowercase DNS-safe and <=26 characters")
    try:
        if args.prepare:
            prepare(args.validation_id)
            print(f"prepared teste/ and audit/final-validation/ for {args.validation_id}")
        else:
            result = build(args.validation_id)
            print(f"overall={result['overall_status']}")
            return 0 if result["overall_status"] == "PASS" else 1
        return 0
    except Exception as exc:
        print(f"audit package: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
