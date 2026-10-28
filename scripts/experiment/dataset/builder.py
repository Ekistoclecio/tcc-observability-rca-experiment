from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import subprocess
from typing import Any

from .backends import PortForwardSet, collect_jaeger, collect_loki, collect_prometheus
from .core import (DatasetError, RAW_SIGNALS, SCENARIOS, Window, build_query_plan,
                   iso_utc, parse_run_id, read_json, sha256_file, window_from_ground_truth,
                   write_json_atomic)
from .normalize import normalize_scenario, scenario_stats


REPO_ROOT = Path(__file__).resolve().parents[3]
EXPERIMENT_ROOT = REPO_ROOT / "artifacts" / "experiments"
DATASET_ROOT = REPO_ROOT / "artifacts" / "datasets"


def _builder_revision() -> str | None:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _load_run(run_id: str, pre_buffer: float, post_buffer: float) -> tuple[Window, dict[str, Any]]:
    parse_run_id(run_id)
    run_dir = EXPERIMENT_ROOT / run_id
    gt_path = run_dir / "ground-truth.json"
    metadata_path = run_dir / "run-metadata.json"
    if not gt_path.is_file() or not metadata_path.is_file():
        raise DatasetError(f"run {run_id} is missing ground-truth.json or run-metadata.json")
    ground_truth = read_json(gt_path)
    run_metadata = read_json(metadata_path)
    if not isinstance(run_metadata, dict) or run_metadata.get("run_id") != run_id:
        raise DatasetError("run metadata run_id does not match the requested run")
    # Causal Ground Truth fields are not retained, returned, or used to build selectors.
    window = window_from_ground_truth(ground_truth, pre_buffer, post_buffer)
    return window, {"run_id": run_id}


def collect_run(run_id: str, pre_buffer: float = 10, post_buffer: float = 10,
                kubectl: str = "kubectl", context: str | None = None,
                loki_limit: int = 100000, jaeger_limit: int = 10000) -> Path:
    if loki_limit < 1 or jaeger_limit < 1:
        raise DatasetError("Loki and Jaeger query limits must be positive")
    window, _ = _load_run(run_id, pre_buffer, post_buffer)
    output = DATASET_ROOT / run_id
    if output.exists():
        raise DatasetError(f"dataset snapshot already exists at {output}; raw snapshots are immutable")
    output.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, Any] = {
        "schema_version": "1.0", "run_id": run_id, "builder_version": "1.0.0",
        "builder_commit": _builder_revision(), "collection_started_at": iso_utc(datetime.now(timezone.utc)),
        "collection_finished_at": None, "window": window.json(),
        "scenarios": {scenario: {"status": "pending", "sources": {}} for scenario in SCENARIOS},
        "raw_sha256": {}, "status": "collecting",
    }
    manifest_path = output / "manifest.json"
    write_json_atomic(manifest_path, manifest)
    try:
        plan = build_query_plan(window)
        with PortForwardSet(kubectl, context) as urls:
            # PortForwardSet.start waits for the HTTP readiness endpoint of every backend.
            for scenario in SCENARIOS:
                scenario_dir = output / f"scenario-{scenario.lower()}" / "raw"
                scenario_dir.mkdir(parents=True, exist_ok=False)
                queries = {item["backend"]: item for item in plan[scenario]}
                raw_results = {
                    "metrics": collect_prometheus(urls[f"{scenario}.prometheus"], window, scenario, queries["prometheus"]),
                    "logs": collect_loki(urls[f"{scenario}.loki"], window, scenario, queries["loki"], loki_limit),
                    "traces": collect_jaeger(urls[f"{scenario}.jaeger"], window, scenario, queries["jaeger"], jaeger_limit),
                }
                for signal, document in raw_results.items():
                    path = scenario_dir / f"{signal}.json"
                    write_json_atomic(path, document)
                    manifest["raw_sha256"][f"scenario-{scenario.lower()}/raw/{signal}.json"] = sha256_file(path)
                    manifest["scenarios"][scenario]["sources"][signal] = {
                        "backend": document["source"], "status": "collected",
                        "query": document["query"],
                    }
                manifest["scenarios"][scenario]["status"] = "collected"
                write_json_atomic(manifest_path, manifest)
        manifest["status"] = "collected"
    except Exception as exc:
        manifest["status"] = "invalid_collection"
        manifest["failure"] = {"type": type(exc).__name__, "message": str(exc)}
        raise
    finally:
        manifest["collection_finished_at"] = iso_utc(datetime.now(timezone.utc))
        write_json_atomic(manifest_path, manifest)
    return output


def normalize_run(run_id: str) -> Path:
    parse_run_id(run_id)
    try:
        return _normalize_run_impl(run_id)
    except Exception as exc:
        manifest_path = DATASET_ROOT / run_id / "manifest.json"
        if manifest_path.is_file():
            try:
                manifest = read_json(manifest_path)
                manifest["status"] = "invalid_normalization"
                manifest["normalization_failure"] = {"type": type(exc).__name__, "message": str(exc)}
                manifest["normalization_finished_at"] = iso_utc(datetime.now(timezone.utc))
                write_json_atomic(manifest_path, manifest)
            except Exception:
                pass
        raise


def _normalize_run_impl(run_id: str) -> Path:
    parse_run_id(run_id)
    output = DATASET_ROOT / run_id
    manifest_path = output / "manifest.json"
    manifest = read_json(manifest_path)
    if manifest.get("run_id") != run_id or manifest.get("status") not in ("collected", "normalized", "invalid_normalization"):
        raise DatasetError("normalization requires a complete collected snapshot")
    window_data = manifest.get("window", {})
    window = Window(
        confirmed_start=_parse_manifest_time(window_data.get("confirmed_start")),
        confirmed_end=_parse_manifest_time(window_data.get("confirmed_end")),
        start=_parse_manifest_time(window_data.get("start")), end=_parse_manifest_time(window_data.get("end")),
        pre_buffer_seconds=float(window_data.get("pre_buffer_seconds", -1)),
        post_buffer_seconds=float(window_data.get("post_buffer_seconds", -1)),
    )
    for scenario in SCENARIOS:
        scenario_root = output / f"scenario-{scenario.lower()}"
        raw_dir = scenario_root / "raw"
        if manifest.get("scenarios", {}).get(scenario, {}).get("status") not in ("collected", "normalized", "invalid_normalization"):
            raise DatasetError(f"scenario {scenario} is not fully collected")
        raw_signals: dict[str, Any] = {}
        raw_bytes: dict[str, int] = {}
        for signal in RAW_SIGNALS:
            relative = f"scenario-{scenario.lower()}/raw/{signal}.json"
            path = raw_dir / f"{signal}.json"
            if not path.is_file():
                raise DatasetError(f"missing raw snapshot: {path}")
            expected_hash = manifest.get("raw_sha256", {}).get(relative)
            actual_hash = sha256_file(path)
            if expected_hash != actual_hash:
                raise DatasetError(f"raw snapshot hash mismatch: {relative}")
            raw_signals[signal] = read_json(path)
            raw_bytes[signal] = path.stat().st_size
        dataset = normalize_scenario(run_id, scenario, window, raw_signals)
        dataset_path = scenario_root / "dataset.json"
        write_json_atomic(dataset_path, dataset)
        normalized_bytes = dataset_path.stat().st_size
        stats = scenario_stats(dataset, raw_bytes, normalized_bytes)
        write_json_atomic(scenario_root / "stats.json", stats)
        manifest["scenarios"][scenario]["status"] = "normalized"
        manifest.setdefault("normalized_sha256", {})[f"scenario-{scenario.lower()}/dataset.json"] = sha256_file(dataset_path)
        manifest["normalized_sha256"][f"scenario-{scenario.lower()}/stats.json"] = sha256_file(scenario_root / "stats.json")
    manifest["status"] = "normalized"
    manifest["normalization_finished_at"] = iso_utc(datetime.now(timezone.utc))
    write_json_atomic(manifest_path, manifest)
    return output


def _parse_manifest_time(value: Any):
    from .core import parse_timestamp
    try:
        return parse_timestamp(value, "manifest window")
    except DatasetError as exc:
        raise DatasetError(f"invalid manifest time: {exc}") from exc


def build_run(run_id: str, pre_buffer: float = 10, post_buffer: float = 10,
              kubectl: str = "kubectl", context: str | None = None,
              loki_limit: int = 100000, jaeger_limit: int = 10000) -> Path:
    collect_run(run_id, pre_buffer, post_buffer, kubectl, context, loki_limit, jaeger_limit)
    return normalize_run(run_id)
