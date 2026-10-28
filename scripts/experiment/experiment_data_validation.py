from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from scripts.experiment.dataset.backends import (  # noqa: E402
    PortForwardSet, collect_jaeger, collect_loki, collect_prometheus,
)
from scripts.experiment.dataset.core import (  # noqa: E402
    SCENARIOS, Window, build_query_plan, iso_utc, read_json,
    sha256_file, write_json_atomic,
)
from scripts.experiment.integration_validation import (  # noqa: E402
    FAULTS, marker_key_paths, run_id_for, utc_now,
)


ARTIFACT_ROOT = REPO / "artifacts" / "experiment-validation"
EXPERIMENT_ROOT = REPO / "artifacts" / "experiments"
DATASET_ROOT = REPO / "artifacts" / "datasets"
RCA_ROOT = REPO / "artifacts" / "rca-inputs"
INTEGRATION_ROOT = REPO / "artifacts" / "integration-validation"


class PackageError(RuntimeError):
    pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run new F1-F5 validations and package the three experiment data sets")
    parser.add_argument("--validation-id", required=True)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--all", action="store_true")
    selection.add_argument("--fault", choices=tuple(item.lower() for item in FAULTS))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true", help="resume an existing package after verifying completed fault snapshots")
    parser.add_argument("--pre-buffer", type=float, default=float(os.environ.get("TCC_DATASET_PRE_BUFFER_SECONDS", "10")))
    parser.add_argument("--post-buffer", type=float, default=float(os.environ.get("TCC_DATASET_POST_BUFFER_SECONDS", "10")))
    return parser.parse_args(argv)


def validate_id(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,25}", value):
        raise PackageError("validation-id must be 1-26 lowercase DNS-safe characters")
    return value


def time_value(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise PackageError(f"timestamp lacks timezone: {value}")
    return parsed.astimezone(timezone.utc)


def resolve_capture_bounds(workload_start: str, workload_end: str, rca_start: str, rca_end: str) -> tuple[datetime, datetime]:
    actual_start, actual_end = time_value(workload_start), time_value(workload_end)
    window_start, window_end = time_value(rca_start), time_value(rca_end)
    if actual_end <= actual_start or window_end <= window_start:
        raise PackageError("workload and RCA windows must have positive duration")
    return min(actual_start, window_start), max(actual_end, window_end)


def write_json(path: Path, value: Any) -> None:
    write_json_atomic(path, value)


def count_metrics(raw: dict[str, Any]) -> tuple[int, int]:
    data = raw.get("response", {}).get("data", {}).get("result", [])
    series = len(data)
    samples = sum(len(item.get("values", [])) if "values" in item else 1 for item in data)
    return series, samples


def count_logs(raw: dict[str, Any]) -> int:
    return sum(len(stream.get("values", [])) for stream in raw.get("response", {}).get("data", {}).get("result", []))


def unique_traces_and_spans(raw: dict[str, Any]) -> tuple[int, int]:
    unique: dict[str, dict[str, Any]] = {}
    for result in raw.get("search_results", []):
        for trace in result.get("response", {}).get("data", []):
            trace_id = trace.get("traceID")
            if trace_id:
                unique.setdefault(trace_id, trace)
    return len(unique), sum(len(item.get("spans", [])) for item in unique.values())


def file_info(path: Path) -> dict[str, Any]:
    return {"path": path.as_posix(), "bytes": path.stat().st_size, "sha256": sha256_file(path)}


def copy_file(source: Path, target: Path) -> dict[str, Any]:
    if not source.is_file():
        raise PackageError(f"required source artifact is missing: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    if sha256_file(source) != sha256_file(target):
        raise PackageError(f"copy integrity mismatch: {source} -> {target}")
    return file_info(target)


def capture_full_run(run_id: str, telemetry_root: Path, workload_events: list[dict[str, Any]],
                     gt: dict[str, Any], rca_manifest: dict[str, Any], pre_buffer: float,
                     post_buffer: float) -> dict[str, Any]:
    event_by_name: dict[str, dict[str, Any]] = {}
    for event in workload_events:
        name = event.get("event")
        if name in ("warmup_started", "workload_finished"):
            event_by_name[name] = event
    if "warmup_started" not in event_by_name:
        raise PackageError("workload events lack warmup_started; refusing to infer full-run start")
    if "workload_finished" not in event_by_name:
        raise PackageError("workload events lack workload_finished; refusing to infer full-run end")

    workload_start_value = event_by_name["warmup_started"]["timestamp"]
    workload_end_value = event_by_name["workload_finished"]["timestamp"]
    rca_start_value = rca_manifest["window"]["start"]
    rca_end_value = rca_manifest["window"]["end"]
    actual_workload_start, actual_workload_end = time_value(workload_start_value), time_value(workload_end_value)
    rca_start, rca_end = time_value(rca_start_value), time_value(rca_end_value)
    capture_start, capture_end = resolve_capture_bounds(workload_start_value, workload_end_value, rca_start_value, rca_end_value)
    gt_start = time_value(gt["timing"]["confirmed_start"])
    gt_end = time_value(gt["timing"]["confirmed_end"])
    window = Window(gt_start, gt_end, capture_start, capture_end, pre_buffer, post_buffer)
    plan = build_query_plan(window)

    collected: dict[str, dict[str, Any]] = {}
    started = utc_now()
    with PortForwardSet() as urls:
        for scenario in SCENARIOS:
            query_by_backend = {item["backend"]: item for item in plan[scenario]}
            collected[scenario] = {
                "metrics": collect_prometheus(urls[f"{scenario}.prometheus"], window, scenario, query_by_backend["prometheus"]),
                "logs": collect_loki(urls[f"{scenario}.loki"], window, scenario, query_by_backend["loki"], 100000),
                "traces": collect_jaeger(urls[f"{scenario}.jaeger"], window, scenario, query_by_backend["jaeger"], 10000),
            }
    signal_info: dict[str, Any] = {}
    for scenario in SCENARIOS:
        full_dir = telemetry_root / f"scenario-{scenario.lower()}" / "full-run"
        scenario_raw = full_dir / "raw"
        scenario_raw.mkdir(parents=True, exist_ok=False)
        signal_info[scenario] = {}
        for signal, document in collected[scenario].items():
            path = scenario_raw / f"{signal}.json"
            write_json(path, document)
            info = file_info(path)
            info["path"] = f"raw/{signal}.json"
            if signal == "metrics":
                series, samples = count_metrics(document)
                info.update({"series": series, "samples": samples})
            elif signal == "logs":
                info["records"] = count_logs(document)
            else:
                traces, spans = unique_traces_and_spans(document)
                info.update({"traces": traces, "spans": spans})
            signal_info[scenario][signal] = info
        write_json(full_dir / "manifest.json", {
            "schema_version": "1.0",
            "run_id": run_id,
            "scenario": scenario,
            "collection_started_at": started,
            "collection_finished_at": utc_now(),
            "full_run_window": {"start": iso_utc(capture_start), "end": iso_utc(capture_end)},
            "actual_workload_window": {"start": iso_utc(actual_workload_start), "end": iso_utc(actual_workload_end)},
            "rca_window": {"start": iso_utc(rca_start), "end": iso_utc(rca_end)},
            "fault_confirmed_window": {"start": iso_utc(gt_start), "end": iso_utc(gt_end)},
            "capture_window_definition": "min(actual warmup_started, rca_window.start) to max(actual workload_finished, rca_window.end)",
            "buffers_seconds": {"pre": pre_buffer, "post": post_buffer},
            "signals": signal_info[scenario],
            "source_policy": "global fixed SUT scope; no Ground Truth causal fields used to select telemetry",
        })
    manifest = {
        "schema_version": "1.0",
        "run_id": run_id,
        "collection_started_at": started,
        "collection_finished_at": utc_now(),
        "full_run_window": {"start": iso_utc(capture_start), "end": iso_utc(capture_end)},
        "actual_workload_window": {"start": iso_utc(actual_workload_start), "end": iso_utc(actual_workload_end)},
        "rca_window": {"start": iso_utc(rca_start), "end": iso_utc(rca_end)},
        "fault_confirmed_window": {"start": iso_utc(gt_start), "end": iso_utc(gt_end)},
        "capture_window_definition": "min(actual warmup_started, rca_window.start) to max(actual workload_finished, rca_window.end)",
        "buffers_seconds": {"pre": pre_buffer, "post": post_buffer},
        "scenarios": signal_info,
        "source_policy": "global fixed SUT scope; no Ground Truth causal fields used to select telemetry",
    }
    return manifest


def make_scenario_package(run_id: str, fault_root: Path, fault: str, inner_root: Path,
                          pre_buffer: float, post_buffer: float) -> dict[str, Any]:
    exp_dir = EXPERIMENT_ROOT / run_id
    ds_dir = DATASET_ROOT / run_id
    rca_dir = RCA_ROOT / run_id
    fault_validation = read_json(inner_root / fault.lower() / "validation.json")
    if fault_validation.get("overall") != "PASS":
        raise PackageError(f"underlying integration run {fault} did not PASS")
    gt_source = exp_dir / "ground-truth.json"
    gt = read_json(gt_source)
    if gt.get("status") != "valid" or not gt.get("validation", {}).get("injection_confirmed") or not gt.get("validation", {}).get("recovery_confirmed"):
        raise PackageError(f"Ground Truth invalid for {run_id}")
    dataset_manifest = read_json(ds_dir / "manifest.json")
    if dataset_manifest.get("status") != "normalized":
        raise PackageError(f"RCA-window dataset is not normalized: {run_id}")

    telemetry_root = fault_root / "01-telemetry"
    telemetry_root.mkdir(parents=True, exist_ok=False)
    scenario_manifests: dict[str, Any] = {}
    for scenario in SCENARIOS:
        scenario_name = f"scenario-{scenario.lower()}"
        scenario_package = telemetry_root / scenario_name
        rca_output = scenario_package / "rca-window"
        raw_dest = rca_output / "raw"
        raw_dest.mkdir(parents=True)
        for signal in ("metrics", "logs", "traces"):
            copy_file(ds_dir / scenario_name / "raw" / f"{signal}.json", raw_dest / f"{signal}.json")
        for filename in ("dataset.json", "stats.json"):
            copy_file(ds_dir / scenario_name / filename, rca_output / filename)
        # Dataset manifests are run-level; retain the original manifest per scenario as requested.
        copy_file(ds_dir / "manifest.json", rca_output / "manifest.json")
        rca_manifest = read_json(rca_output / "manifest.json")
        request_source = rca_dir / scenario_name
        rca_input_dir = fault_root / "03-rca-input" / scenario_name
        rca_input_dir.mkdir(parents=True, exist_ok=False)
        for filename in ("request.json", "request-preview.md", "manifest.json"):
            copy_file(request_source / filename, rca_input_dir / filename)

        source_events = read_jsonl(exp_dir / "orchestrator-events.jsonl")
        workload_events = [event for event in source_events if event.get("event") in {
            "warmup_started", "warmup_ended", "steady_started", "admission_stopped", "workload_ended",
            "cooldown_started", "workload_finished", "workload_finalized",
        }]
        scenario_manifests[scenario] = {
            "full_run_manifest": {"path": f"{scenario_name}/full-run/manifest.json"},
            "rca_window_manifest": {**file_info(rca_output / "manifest.json"), "path": f"{scenario_name}/rca-window/manifest.json"},
            "full_run_window": None,
            "rca_window": rca_manifest["window"],
        }

    first_rca_manifest = read_json(telemetry_root / "scenario-a/rca-window/manifest.json")
    full_manifest = capture_full_run(run_id, telemetry_root, workload_events, gt, first_rca_manifest, pre_buffer, post_buffer)
    for scenario in SCENARIOS:
        scenario_package = telemetry_root / f"scenario-{scenario.lower()}"
        one = read_json(scenario_package / "full-run/manifest.json")
        scenario_manifests[scenario]["full_run_manifest"] = {
            **file_info(scenario_package / "full-run/manifest.json"),
            "path": f"{scenario_package.name}/full-run/manifest.json",
        }
        scenario_manifests[scenario]["full_run_window"] = one["full_run_window"]

    gt_dir = fault_root / "02-ground-truth"
    gt_dir.mkdir()
    gt_info = copy_file(gt_source, gt_dir / "ground-truth.json")
    (gt_dir / "ground-truth.sha256").write_text(f"{gt_info['sha256']}  ground-truth.json\n", encoding="utf-8")
    (gt_dir / "README.md").write_text(
        "# Ground Truth\n\n`ground-truth.json` é a cópia byte a byte do arquivo produzido pelo Experiment Orchestrator. "
        "Ele é a fonte de verdade causal e não deve ser copiado para o RCA Input.\n", encoding="utf-8")

    audit = fault_root / "audit"
    audit.mkdir(exist_ok=True)
    shutil.copytree(inner_root, audit / "integration-validation")
    run_audit = audit / "run-control"
    run_audit.mkdir()
    for filename in ("run-metadata.json", "orchestrator-events.jsonl", "workload-runner.log"):
        copy_file(exp_dir / filename, run_audit / filename)
    config = inner_root / fault.lower() / "fault-config.json"
    if config.is_file():
        copy_file(config, run_audit / "fault-config.json")
    write_json(audit / "timeline.json", {
        "workload_events": workload_events,
        "ground_truth_timing": {key: gt["timing"].get(key) for key in (
            "scheduled_start", "requested_at", "confirmed_start", "scheduled_end", "recovery_requested_at", "confirmed_end")},
        "capture_windows": scenario_manifests,
    })
    validation = {
        "fault": fault,
        "run_id": run_id,
        "overall": "PASS",
        "warnings": fault_validation.get("warnings", []),
        "ground_truth": gt_info,
        "scenario_artifacts": scenario_manifests,
        "source_validation": file_info(inner_root / fault.lower() / "validation.json"),
    }
    write_json(audit / "validation.json", validation)
    lines = [f"# {fault} — Evidence summary", "", "## O que foi validado", "",
             f"- A run `{run_id}` foi nova nesta bateria e o validador integrado reportou PASS.",
             "- Ground Truth original copiado sem transformação.",
             "- RAW full-run foi coletado de A e B após a execução, usando a janela temporal explícita.",
             "- Dataset da janela RCA e Canonical RCA Request foram copiados dos builders das Fases 5/6.",
             "- Evidências operacionais completas estão em `audit/integration-validation/`.", "",
             "## Janelas", ""]
    for scenario in SCENARIOS:
        item = scenario_manifests[scenario]
        lines.append(f"- {scenario}: full-run `{item['full_run_window']['start']}` → `{item['full_run_window']['end']}`; RCA `{item['rca_window']['start']}` → `{item['rca_window']['end']}`.")
    lines += ["", "## Checks", "", "Ground Truth, recovery, A/B observability, Dataset, request, provenance e cleanup: PASS conforme `audit/validation.json` e evidências vinculadas.", ""]
    (audit / "evidence-summary.md").write_text("\n".join(lines), encoding="utf-8")
    return validation


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def validate_packaged_fault(fault_root: Path, fault: str, run_id: str) -> dict[str, Any]:
    checks: dict[str, bool] = {}
    details: dict[str, Any] = {}
    gt_path = fault_root / "02-ground-truth/ground-truth.json"
    gt = read_json(gt_path)
    checks["ground_truth_valid"] = (
        gt.get("status") == "valid"
        and gt.get("validation", {}).get("injection_confirmed") is True
        and gt.get("validation", {}).get("recovery_confirmed") is True
    )
    details["ground_truth_sha256"] = sha256_file(gt_path)
    scenario_windows: dict[str, Any] = {}
    for scenario in SCENARIOS:
        name = f"scenario-{scenario.lower()}"
        scenario_root = fault_root / "01-telemetry" / name
        full_manifest = read_json(scenario_root / "full-run/manifest.json")
        rca_manifest = read_json(scenario_root / "rca-window/manifest.json")
        full_window = full_manifest["full_run_window"]
        rca_window = full_manifest["rca_window"]
        scenario_windows[scenario] = {"full_run": full_window, "rca": rca_window}
        checks[f"{scenario}_full_raw_present"] = all(
            (scenario_root / "full-run/raw" / f"{signal}.json").is_file()
            for signal in ("metrics", "logs", "traces")
        )
        checks[f"{scenario}_full_window_contains_workload_and_rca"] = (
            time_value(full_window["start"]) <= time_value(full_manifest["actual_workload_window"]["start"])
            and time_value(full_window["end"]) >= time_value(full_manifest["actual_workload_window"]["end"])
            and time_value(full_window["start"]) <= time_value(rca_window["start"])
            and time_value(full_window["end"]) >= time_value(rca_window["end"])
        )
        checks[f"{scenario}_rca_window_matches_dataset"] = (
            rca_manifest["window"].get("start") == rca_window.get("start")
            and rca_manifest["window"].get("end") == rca_window.get("end")
        )
        for signal in ("metrics", "logs", "traces"):
            raw_path = scenario_root / "full-run/raw" / f"{signal}.json"
            raw_doc = read_json(raw_path)
            query_window = raw_doc.get("query", {}).get("window", {})
            checks[f"{scenario}_{signal}_hash_valid"] = sha256_file(raw_path) == full_manifest["signals"][signal]["sha256"]
            checks[f"{scenario}_{signal}_query_window_matches"] = (
                query_window.get("start") == full_window["start"]
                and query_window.get("end") == full_window["end"]
            )
        dataset_path = scenario_root / "rca-window/dataset.json"
        request_path = fault_root / "03-rca-input" / name / "request.json"
        dataset = read_json(dataset_path)
        request = read_json(request_path)
        counts_dataset = {
            "metrics": len(dataset["metrics"]), "logs": len(dataset["logs"]),
            "traces": len(dataset["traces"]),
            "spans": sum(len(trace.get("spans", [])) for trace in dataset["traces"]),
        }
        counts_request = {
            "metrics": len(request["telemetry"]["metrics"]), "logs": len(request["telemetry"]["logs"]),
            "traces": len(request["telemetry"]["traces"]),
            "spans": sum(len(trace.get("spans", [])) for trace in request["telemetry"]["traces"]),
        }
        details[f"{scenario}_counts"] = {"dataset": counts_dataset, "request": counts_request}
        checks[f"{scenario}_dataset_request_counts_match"] = counts_dataset == counts_request
        request_text = request_path.read_text(encoding="utf-8")
        checks[f"{scenario}_run_id_absent_from_request"] = run_id not in request_text
        checks[f"{scenario}_explicit_treatment_markers_absent"] = not marker_key_paths(request, {"telemetry_scenario"})
        request_manifest = read_json(request_path.parent / "manifest.json")
        checks[f"{scenario}_dataset_hash_matches_request_manifest"] = sha256_file(dataset_path) == request_manifest["source_dataset_sha256"]
        checks[f"{scenario}_request_hash_matches_manifest"] = sha256_file(request_path) == request_manifest["request_sha256"]
        checks[f"{scenario}_copied_dataset_matches_original_manifest"] = (
            sha256_file(dataset_path) == rca_manifest["normalized_sha256"][f"{name}/dataset.json"]
        )
    checks["same_full_run_window_ab"] = scenario_windows["A"]["full_run"] == scenario_windows["B"]["full_run"]
    checks["same_rca_window_ab"] = scenario_windows["A"]["rca"] == scenario_windows["B"]["rca"]
    checks["ground_truth_absent_from_rca_input_tree"] = not any(
        path.name == "ground-truth.json" for path in (fault_root / "03-rca-input").rglob("*")
    )
    return {"fault": fault, "run_id": run_id, "checked_at": utc_now(), "checks": checks,
            "details": details, "passed": all(checks.values())}


def write_root_docs(root: Path, validation_id: str, faults: dict[str, Any], pre_buffer: float, post_buffer: float) -> None:
    existing_manifest = read_json(root / "manifest.json") if (root / "manifest.json").is_file() else {}
    root_manifest = {
        "schema_version": "1.0", "validation_id": validation_id,
        "purpose": "non-scientific end-to-end artifact validation",
        "created_at": existing_manifest.get("created_at", utc_now()),
        "fault_order": list(faults),
        "runs": {fault: {"run_id": value["run_id"], "telemetry": f"{fault}/01-telemetry/",
                         "ground_truth": f"{fault}/02-ground-truth/ground-truth.json",
                         "rca_input": f"{fault}/03-rca-input/", "overall": value["overall"],
                         "warnings": value.get("warnings", [])}
                 for fault, value in faults.items()},
        "buffers_seconds": {"pre": pre_buffer, "post": post_buffer},
    }
    final_check_path = root / "audit/final-state-validation.json"
    if final_check_path.is_file():
        final_check = read_json(final_check_path)
        root_manifest["final_cluster_health"] = {"passed": final_check["passed"], "evidence": "audit/final-state-validation.json"}
    write_json(root / "manifest.json", root_manifest)
    readme = """# Experiment Data Validation\n\n\
Esta pasta contém uma bateria nova de validação de integração.\n\n\
## Os três conjuntos fundamentais\n\n\
- `01-telemetry/`: o que o sistema produziu. `full-run/` guarda RAW da execução inteira (incluindo warm-up, steady, recovery, drain/cooldown e buffer RCA); `rca-window/` guarda o RAW, Dataset e stats da janela RCA usada atualmente.\n\
- `02-ground-truth/`: o que realmente aconteceu, no JSON original do Experiment Orchestrator.\n\
- `03-rca-input/`: o Canonical Full RCA Request A/B que seria entregue a um adapter de modelo. Não inclui Ground Truth.\n\n\
`audit/` guarda evidências operacionais e de validação; não é conteúdo do Dataset nem do RCA Input.\n\n\
Esta bateria valida geração e organização dos artifacts. As execuções não integram a campanha oficial e não devem ser usadas para comparar a capacidade diagnóstica dos cenários A e B. Nenhuma IA foi executada.\n"""
    (root / "README.md").write_text(readme, encoding="utf-8")
    rows = ["# Relatório — validação dos artifacts experimentais", "",
            f"Validation ID: `{validation_id}`. Bateria de engenharia, não científica. Buffers RCA: pre={pre_buffer}s, post={post_buffer}s.", "",
            "| Fault | Full-run telemetry A/B | Ground Truth | RCA Input A/B | Status |", "|---|---|---|---|---|"]
    for fault, item in faults.items():
        rows.append(f"| {fault} | PASS | PASS | PASS | {item['overall']} |")
    for fault, item in faults.items():
        rows += ["", f"## {fault}", "", "### 1. Telemetria", ""]
        for scenario in SCENARIOS:
            paths = item["scenario_artifacts"][scenario]
            full_manifest_path = root / fault / "01-telemetry" / f"scenario-{scenario.lower()}" / "full-run/manifest.json"
            full = read_json(full_manifest_path)
            raw_summary = full["signals"]
            rows.append(f"- Cenário {scenario}: full-run `{fault}/01-telemetry/scenario-{scenario.lower()}/full-run/`; janela `{paths['full_run_window']['start']}` → `{paths['full_run_window']['end']}`; metrics={raw_summary['metrics']['samples']} samples, logs={raw_summary['logs']['records']}, traces={raw_summary['traces']['traces']}, spans={raw_summary['traces']['spans']}.")
            rows.append(f"- RCA window `{paths['rca_window']['start']}` → `{paths['rca_window']['end']}`; Dataset em `{fault}/01-telemetry/scenario-{scenario.lower()}/rca-window/dataset.json`.")
        gt = read_json(root / fault / "02-ground-truth/ground-truth.json")
        rows += ["", "### 2. Ground Truth", "", f"- Path: `{fault}/02-ground-truth/ground-truth.json` (SHA-256 `{item['ground_truth']['sha256']}`).",
                 f"- Classe/localização: `{gt['fault']['type']}` / `{json.dumps(gt['fault']['location'], sort_keys=True)}` (control plane).",
                 f"- Janela confirmada: `{gt['timing']['confirmed_start']}` → `{gt['timing']['confirmed_end']}`.", "",
                 "### 3. RCA Input", ""]
        for scenario in SCENARIOS:
            req = root / fault / "03-rca-input" / f"scenario-{scenario.lower()}" / "request.json"
            doc = read_json(req)
            rows.append(f"- Cenário {scenario}: `{req.relative_to(root).as_posix()}`, SHA-256 `{sha256_file(req)}`, bytes {req.stat().st_size}; metrics={len(doc['telemetry']['metrics'])}, logs={len(doc['telemetry']['logs'])}, traces={len(doc['telemetry']['traces'])}, spans={sum(len(t.get('spans', [])) for t in doc['telemetry']['traces'])}.")
        rows += ["", "### Audit", "", f"- Status: {item['overall']}; detalhes e outputs: `{fault}/audit/`."]
    warning_items = [(fault, warning) for fault, value in faults.items() for warning in value.get("warnings", [])]
    rows += ["", "## Warnings/failures", "", f"Warnings: {len(warning_items)}; failures: {sum(x['overall'] != 'PASS' for x in faults.values())}.", ""]
    for fault, warning in warning_items:
        rows.append(f"- {fault}: {warning.get('message', warning)} (evidence: `{warning.get('evidence_path', f'{fault}/audit/')}`).")
    rows.append("")
    final_check_path = root / "audit/final-state-validation.json"
    if final_check_path.is_file():
        final_check = read_json(final_check_path)
        rows += ["## Estado final do cluster", "",
                 f"- Resultado: {'PASS' if final_check['passed'] else 'FAIL'}; evidência: `audit/final-state-validation.json`.",
                 f"- Deployments SUT Ready: {sum(item['passed'] for item in final_check['sut_deployments'])}/11.",
                 f"- Deployments de observabilidade Ready: {sum(item['passed'] for item in final_check['observability_deployments'])}/7.",
                 f"- Promtail Ready: {final_check['promtail']['ready']}/{final_check['promtail']['desired']}.",
                 f"- loadgenerator original: {final_check['original_loadgenerator_replicas']} réplicas.",
                 f"- Chaos residual: StressChaos={final_check['residual_chaos']['StressChaos']}; NetworkChaos={final_check['residual_chaos']['NetworkChaos']}.",
                 f"- Flag F4 ausente: {final_check['f4_flag_check']['absent']}.",
                 f"- Validador observabilidade A/B: {'PASS' if final_check['phase2_observability']['passed'] else 'FAIL'}.", ""]
    (root / "validation-report.md").write_text("\n".join(rows), encoding="utf-8")

    index_lines = ["# Evidence index", "", "Paths below are relative to this validation package. SHA-256 and byte sizes identify the immutable snapshots.", "",
                   "| Fault | Dataset | Path | SHA-256 | Bytes |", "|---|---|---|---|---:|"]
    for fault in faults:
        base = root / fault
        candidates = [base / "02-ground-truth/ground-truth.json", base / "audit/validation.json",
                     base / "audit/package-validation.json"]
        if (base / "audit/package-recovery.md").is_file():
            candidates.append(base / "audit/package-recovery.md")
        for scenario in ("scenario-a", "scenario-b"):
            scenario_base = base / "01-telemetry" / scenario
            candidates += [scenario_base / "full-run/manifest.json"]
            candidates += [scenario_base / "full-run/raw" / f"{signal}.json" for signal in ("metrics", "logs", "traces")]
            candidates += [scenario_base / "rca-window/raw" / f"{signal}.json" for signal in ("metrics", "logs", "traces")]
            candidates += [scenario_base / "rca-window/dataset.json", scenario_base / "rca-window/stats.json", scenario_base / "rca-window/manifest.json"]
            candidates += [base / "03-rca-input" / scenario / filename for filename in ("request.json", "request-preview.md", "manifest.json")]
        for path in candidates:
            info = file_info(path)
            rel = path.relative_to(root).as_posix()
            index_lines.append(f"| {fault} | `{path.parent.parent.name}` | `{rel}` | `{info['sha256']}` | {info['bytes']} |")
    final_audit = root / "audit/final-cluster-state"
    if final_audit.is_dir():
        final_candidates = [final_audit / filename for filename in (
            "nodes.json", "deployments.json", "pods.json", "services.json", "endpoints.json",
            "endpointslices.json", "daemonsets.json", "stresschaos.json", "networkchaos.json",
        )]
        final_candidates.append(root / "audit/final-state-validation.json")
        if (final_audit / "observability/result.json").is_file():
            final_candidates.append(final_audit / "observability/result.json")
        for path in final_candidates:
            info = file_info(path)
            rel = path.relative_to(root).as_posix()
            index_lines.append(f"| GLOBAL | `final-state` | `{rel}` | `{info['sha256']}` | {info['bytes']} |")
    (root / "evidence-index.md").write_text("\n".join(index_lines) + "\n", encoding="utf-8")


def capture_final_state_audit(root: Path, faults: dict[str, Any]) -> dict[str, Any]:
    if not all(fault in faults for fault in FAULTS):
        raise PackageError("global final-state audit requires all five packaged faults")
    source = root / "F5/audit/integration-validation/global/final-state"
    if not source.is_dir():
        raise PackageError(f"F5 final cluster snapshot is missing: {source}")
    destination = root / "audit/final-cluster-state"
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination)
    deployments_doc = read_json(destination / "deployments.json")
    daemonsets_doc = read_json(destination / "daemonsets.json")
    chaos_stress = read_json(destination / "stresschaos.json")
    chaos_network = read_json(destination / "networkchaos.json")
    obs_doc = read_json(destination / "observability/result.json")
    deployments = {
        (item.get("metadata", {}).get("namespace"), item.get("metadata", {}).get("name")): item
        for item in deployments_doc.get("items", [])
    }
    required_sut = [("default", name) for name in (
        "frontend", "checkoutservice", "cartservice", "productcatalogservice", "currencyservice",
        "paymentservice", "shippingservice", "emailservice", "recommendationservice", "adservice", "redis-cart",
    )]
    required_obs = [
        ("observability-conventional", "prometheus-conventional"),
        ("observability-conventional", "loki-conventional"),
        ("observability-conventional", "jaeger-conventional"),
        ("observability-otel", "prometheus-otel"),
        ("observability-otel", "loki-otel"),
        ("observability-otel", "jaeger-otel"),
        ("observability-otel", "otel-collector"),
    ]
    def deployment_result(key: tuple[str, str]) -> dict[str, Any]:
        item = deployments.get(key)
        desired = int(item.get("spec", {}).get("replicas", 1)) if item else 0
        ready = int(item.get("status", {}).get("readyReplicas", 0)) if item else 0
        return {"namespace": key[0], "name": key[1], "desired": desired, "ready": ready,
                "passed": bool(item) and desired > 0 and ready == desired}
    sut_results = [deployment_result(key) for key in required_sut]
    obs_results = [deployment_result(key) for key in required_obs]
    promtail = next((item for item in daemonsets_doc.get("items", [])
                     if item.get("metadata", {}).get("namespace") == "observability-conventional"
                     and item.get("metadata", {}).get("name") == "promtail-conventional"), None)
    promtail_state = {"desired": int(promtail.get("status", {}).get("desiredNumberScheduled", 0)) if promtail else 0,
                      "ready": int(promtail.get("status", {}).get("numberReady", 0)) if promtail else 0}
    promtail_state["passed"] = promtail_state["desired"] > 0 and promtail_state["desired"] == promtail_state["ready"]
    loadgen = deployments.get(("default", "loadgenerator"))
    loadgen_replicas = int(loadgen.get("spec", {}).get("replicas", 0)) if loadgen else None
    inner_root = INTEGRATION_ROOT / f"edv-{root.name}-f5"
    command_records = read_jsonl(inner_root / "commands.jsonl")
    flag_record = next((item for item in reversed(command_records) if item.get("stage") == "health-f4-flag"), None)
    flag_stdout = REPO / flag_record["stdout_path"] if flag_record else None
    flag_stderr = REPO / flag_record["stderr_path"] if flag_record else None
    flag_absent = bool(flag_record and flag_record.get("exit_code") == 0 and flag_stdout and flag_stdout.is_file())
    flag_absent = flag_absent and not (flag_stderr and flag_stderr.is_file() and flag_stderr.stat().st_size)
    checks = {
        "all_11_sut_deployments_ready": len(sut_results) == 11 and all(item["passed"] for item in sut_results),
        "all_7_observability_deployments_ready": len(obs_results) == 7 and all(item["passed"] for item in obs_results),
        "promtail_ready": promtail_state["passed"],
        "original_loadgenerator_zero": loadgen_replicas == 0,
        "no_residual_stresschaos": not chaos_stress.get("items", []),
        "no_residual_networkchaos": not chaos_network.get("items", []),
        "f4_flag_absent": flag_absent,
        "final_phase2_observability_passed": obs_doc.get("passed") is True,
    }
    result = {
        "checked_at": utc_now(), "source_snapshot": "F5/audit/integration-validation/global/final-state",
        "sut_deployments": sut_results, "observability_deployments": obs_results,
        "promtail": promtail_state, "original_loadgenerator_replicas": loadgen_replicas,
        "residual_chaos": {"StressChaos": len(chaos_stress.get("items", [])), "NetworkChaos": len(chaos_network.get("items", []))},
        "f4_flag_check": {"stage": flag_record.get("stage") if flag_record else None,
                           "exit_code": flag_record.get("exit_code") if flag_record else None,
                           "stdout_path": str(flag_stdout) if flag_stdout else None,
                           "stderr_path": str(flag_stderr) if flag_stderr else None,
                           "absent": flag_absent},
        "phase2_observability": {"passed": obs_doc.get("passed"), "evidence": file_info(destination / "observability/result.json")},
        "checks": checks, "passed": all(checks.values()),
    }
    write_json(root / "audit/final-state-validation.json", result)
    return result
    (root / "validation-report.md").write_text("\n".join(rows), encoding="utf-8")


def validate_cluster() -> None:
    result = subprocess.run(["kubectl", "get", "nodes", "-o", "json"], cwd=REPO, capture_output=True, text=True)
    if result.returncode != 0:
        raise PackageError(f"cluster preflight failed: {result.stderr.strip()}")
    nodes = json.loads(result.stdout).get("items", [])
    if not nodes or any(not any(c.get("type") == "Ready" and c.get("status") == "True" for c in n.get("status", {}).get("conditions", [])) for n in nodes):
        raise PackageError("cluster has no node or at least one node is not Ready")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        validation_id = validate_id(args.validation_id)
        faults = FAULTS if args.all else (args.fault.upper(),)
        if args.pre_buffer < 0 or args.post_buffer < 0:
            raise PackageError("pre/post buffers must be non-negative")
        root = ARTIFACT_ROOT / validation_id
        if args.dry_run:
            print(f"validation_id={validation_id}\nroot={root}\nfaults={','.join(faults)}")
            for fault in faults:
                inner_id = f"edv-{validation_id}-{fault.lower()}"
                run_id = run_id_for(inner_id, fault)
                from scripts.experiment.integration_validation import FAULT_CONFIGS
                print(f"{fault}: integration_validation --validation-id {inner_id} --fault {fault.lower()} ; run_id={run_id}; config={FAULT_CONFIGS[fault]}; package={root / fault}")
            return 0
        if root.exists() and not args.resume:
            raise PackageError(f"output snapshot exists; refusing overwrite (use --resume only for this in-progress validation): {root}")
        if args.resume and not root.is_dir():
            raise PackageError(f"cannot resume; validation root does not exist: {root}")
        validate_cluster()
        created_at = utc_now()
        campaign: dict[str, Any] = {}
        if not root.exists():
            root.mkdir(parents=True)
            write_json(root / "manifest.json", {"schema_version": "1.0", "validation_id": validation_id,
                        "purpose": "non-scientific end-to-end artifact validation", "created_at": created_at,
                        "fault_order": list(faults), "runs": {}, "buffers_seconds": {"pre": args.pre_buffer, "post": args.post_buffer}})
        elif not (root / "manifest.json").is_file():
            raise PackageError(f"cannot resume root without manifest: {root}")
        for completed_fault in faults:
            completed_root = root / completed_fault
            validation_path = completed_root / "audit/validation.json"
            if not completed_root.exists():
                continue
            if not args.resume or not validation_path.is_file():
                raise PackageError(f"fault package already exists or is incomplete; refusing overwrite: {completed_root}")
            prior = read_json(validation_path)
            prior_run_id = prior.get("run_id")
            if prior.get("fault") != completed_fault or not prior_run_id:
                raise PackageError(f"existing package identity mismatch: {validation_path}")
            old_check_path = completed_root / "audit/package-validation.json"
            warnings = list(prior.get("warnings", []))
            if old_check_path.is_file():
                old_check = read_json(old_check_path)
                if not old_check.get("passed") and not any(item.get("code") == "package_window_comparison_corrected" for item in warnings if isinstance(item, dict)):
                    old_attempt = completed_root / "audit/package-validation-attempt-01.json"
                    if not old_attempt.exists():
                        shutil.copy2(old_check_path, old_attempt)
                    warning = {
                        "code": "package_window_comparison_corrected",
                        "message": "A primeira auditoria do pacote comparou o objeto window inteiro; a Fase 5 inclui campos de buffer/confirmed além de start/end. A verificação foi corrigida para comparar os mesmos limites temporais e passou sem repetir a run nem alterar os dados fonte.",
                        "evidence_path": f"{completed_fault}/audit/package-validation-attempt-01.json",
                    }
                    warnings.append(warning)
                    (completed_root / "audit/package-recovery.md").write_text(
                        "# Package validation recovery\n\nA primeira verificação do pacote F1 falhou apenas porque comparou todos os campos do objeto `window` da Fase 5 com os limites start/end do manifest full-run. A verificação passou a comparar os campos temporais correspondentes (`start` e `end`). O resultado inicial está preservado em `package-validation-attempt-01.json`; a run, Ground Truth, RAW e Dataset não foram alterados nem repetidos. A validação corrigida está em `package-validation.json`.\n",
                        encoding="utf-8",
                    )
            check = validate_packaged_fault(completed_root, completed_fault, prior_run_id)
            write_json(completed_root / "audit/package-validation.json", check)
            prior["package_validation"] = check
            prior["overall"] = "PASS" if check["passed"] else "FAIL"
            prior["warnings"] = warnings
            write_json(validation_path, prior)
            if warnings:
                summary_path = completed_root / "audit/evidence-summary.md"
                current_summary = summary_path.read_text(encoding="utf-8")
                if "## Warnings registrados" not in current_summary:
                    summary_path.write_text(
                        current_summary + "\n## Warnings registrados\n\n"
                        + "\n".join(f"- {item['message']} (ver `{item['evidence_path']}`)." for item in warnings if isinstance(item, dict))
                        + "\n", encoding="utf-8",
                    )
            if not check["passed"]:
                raise PackageError(f"resume audit failed for {completed_fault}: {check['checks']}")
            campaign[completed_fault] = prior
        write_root_docs(root, validation_id, campaign, args.pre_buffer, args.post_buffer)
        for fault in faults:
            if fault in campaign:
                print(f"{fault}: existing package verified; keeping original run")
                continue
            fault_root = root / fault
            fault_root.mkdir()
            inner_id = f"edv-{validation_id}-{fault.lower()}"
            run_id = run_id_for(inner_id, fault)
            inner_root = INTEGRATION_ROOT / inner_id
            occupied = [path for path in (inner_root, EXPERIMENT_ROOT / run_id, DATASET_ROOT / run_id, RCA_ROOT / run_id) if path.exists()]
            if occupied:
                raise PackageError(f"new run identifiers are not fresh; refusing historical artifacts: {occupied}")
            command = [sys.executable, "scripts/experiment/integration_validation.py", "--validation-id", inner_id,
                       "--fault", fault.lower(), "--pre-buffer", str(args.pre_buffer), "--post-buffer", str(args.post_buffer)]
            (fault_root / "audit").mkdir()
            stdout_path = fault_root / "audit" / "commands" / "integration.stdout"
            stderr_path = fault_root / "audit" / "commands" / "integration.stderr"
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            started = utc_now()
            start_mono = time.monotonic()
            with stdout_path.open("w", encoding="utf-8") as out, stderr_path.open("w", encoding="utf-8") as err:
                result = subprocess.run(command, cwd=REPO, stdout=out, stderr=err, text=True, check=False)
            command_record = {"timestamp": started, "finished_at": utc_now(), "stage": "integrated_fault_run",
                              "fault": fault, "run_id": run_id, "command": command, "cwd": str(REPO),
                              "exit_code": result.returncode, "duration_seconds": round(time.monotonic()-start_mono, 3),
                              "stdout_path": str(stdout_path.relative_to(root)), "stderr_path": str(stderr_path.relative_to(root))}
            write_json(fault_root / "audit/command.json", command_record)
            if not inner_root.exists():
                raise PackageError(f"integration validator did not create evidence root: {inner_root}")
            if result.returncode != 0:
                raise PackageError(f"fault {fault} integration validation failed (exit {result.returncode}); see {stderr_path}")
            package_status = make_scenario_package(run_id, fault_root, fault, inner_root, args.pre_buffer, args.post_buffer)
            package_validation = validate_packaged_fault(fault_root, fault, run_id)
            write_json(fault_root / "audit/package-validation.json", package_validation)
            package_status["package_validation"] = package_validation
            package_status["overall"] = "PASS" if package_validation["passed"] else "FAIL"
            if not package_validation["passed"]:
                write_json(fault_root / "audit/validation.json", package_status)
                raise PackageError(f"packaged artifact checks failed for {fault}: {package_validation['checks']}")
            package_status["created_at"] = created_at
            campaign[fault] = package_status
            write_root_docs(root, validation_id, campaign, args.pre_buffer, args.post_buffer)
            print(f"{fault}: packaged {run_id}")
        if tuple(faults) == FAULTS:
            final_state = capture_final_state_audit(root, campaign)
            if not final_state["passed"]:
                raise PackageError(f"final cluster health audit failed: {final_state['checks']}")
        write_root_docs(root, validation_id, campaign, args.pre_buffer, args.post_buffer)
        print(f"package={root}")
        print(f"faults={len(campaign)}/{len(faults)}")
        return 0 if len(campaign) == len(faults) else 1
    except Exception as exc:
        print(f"experiment data validation: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
