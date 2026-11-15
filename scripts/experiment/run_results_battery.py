from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from scripts.experiment.dataset.backends import (  # noqa: E402
    PortForwardSet, collect_jaeger, collect_loki, collect_prometheus,
)
from scripts.experiment.dataset.core import (  # noqa: E402
    SCENARIOS, Window, build_query_plan, epoch_nanoseconds, iso_utc, parse_timestamp, read_json,
    sha256_file, write_json_atomic,
)
from scripts.experiment.experiment_data_validation import (  # noqa: E402
    ARTIFACT_ROOT, count_logs, count_metrics, file_info, read_jsonl, unique_traces_and_spans,
)
from scripts.experiment.integration_validation import (  # noqa: E402
    FAULTS, run_id_for, utc_now,
)

RESULTS_ROOT = REPO / "audit" / "results"
FAULT_TYPES = {
    "F1": "cpu_saturation",
    "F2": "network_latency",
    "F3": "service_unavailable",
    "F4": "application_exception",
    "F5": "datastore_unavailable",
}
GLOBAL_LOKI_SEGMENT_SECONDS = 15
GLOBAL_JAEGER_SEGMENT_SECONDS = 60


class ResultsError(RuntimeError):
    pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a new fault battery and publish the latest results under audit/results")
    parser.add_argument("--validation-id", required=True, help="short unique prefix for fresh control-plane run IDs")
    mode = parser.add_mutually_exclusive_group(required=False)
    mode.add_argument("--all", action="store_true")
    mode.add_argument("--fault", action="append", choices=tuple(f.lower() for f in FAULTS),
                      help="fault to execute; repeat the option to schedule repetitions in the given order")
    mode.add_argument("--resume-global", action="store_true",
                      help="resume global RAW capture for an in-progress battery; does not rerun faults or clean fault folders")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--pre-buffer", type=float, default=10.0)
    parser.add_argument("--post-buffer", type=float, default=10.0)
    return parser.parse_args(argv)


def validate_validation_id(value: str, repetitions: int) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", value):
        raise ResultsError("validation-id must contain lowercase DNS-safe characters")
    longest_child = len(value) + len(f"-{repetitions:03d}")
    # Phase 5's package runner prefixes this with `edv-` and appends `-fN`.
    if longest_child + len("edv-") + len("-f5") > 26:
        raise ResultsError("validation-id is too long; keep it short enough for fresh Phase 5 run IDs")
    return value


def selected_faults(args: argparse.Namespace) -> tuple[str, ...]:
    return FAULTS if args.all else tuple(item.upper() for item in args.fault)


def fault_run_names(faults: tuple[str, ...]) -> list[dict[str, Any]]:
    repetitions: Counter[str] = Counter()
    runs = []
    for sequence, fault in enumerate(faults, start=1):
        if fault not in FAULT_TYPES:
            raise ResultsError(f"unsupported fault class: {fault}")
        repetitions[fault] += 1
        fault_type = FAULT_TYPES[fault]
        runs.append({"fault": fault, "fault_type": fault_type, "sequence": sequence,
                     "repetition": repetitions[fault],
                     "directory": f"{sequence:03d}_{fault_type}_r{repetitions[fault]:02d}"})
    return runs


def expected_results_path(repo_root: Path) -> Path:
    return repo_root.resolve() / "audit" / "results"


def clean_results_only(repo_root: Path, target: Path | None = None) -> Path:
    repo = repo_root.resolve()
    expected = expected_results_path(repo)
    audit_dir = repo / "audit"
    if audit_dir.is_symlink() or audit_dir.resolve(strict=False) != repo / "audit":
        raise ResultsError(f"refusing cleanup because audit path is not the repository audit directory: {audit_dir}")
    requested = target if target is not None else expected
    if requested.is_symlink():
        raise ResultsError(f"refusing cleanup of a symlink: {requested}")
    resolved = requested.resolve(strict=False)
    if resolved != expected or resolved == repo or resolved.parent != audit_dir:
        raise ResultsError(f"refusing cleanup outside the exact repository path {expected}: {requested}")
    if resolved.exists():
        if not resolved.is_dir():
            raise ResultsError(f"results path exists and is not a directory: {resolved}")
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True)
    return resolved


def write_json(path: Path, value: Any) -> None:
    write_json_atomic(path, value)


def global_capture_window(experiment_start: str, experiment_end: str) -> Window:
    start = parse_timestamp(experiment_start, "experiment_start")
    end = parse_timestamp(experiment_end, "experiment_end")
    if end <= start:
        raise ResultsError("experiment_end must be later than experiment_start")
    return Window(start, end, start, end, 0.0, 0.0)


def collect_global_loki(base_url: str, window: Window, scenario: str, query: dict[str, Any]) -> dict[str, Any]:
    """Collect a full-window Loki snapshot in bounded slices.

    This is specific to the global results package. The Phase 5 collector and its
    per-fault query semantics remain unchanged. Adjacent intervals overlap at
    their boundary and the merge deduplicates identical stream/timestamp/line rows.
    Transient failed slices are recursively subdivided until they can be collected.
    """
    segments: list[dict[str, Any]] = []

    def collect(part: Window, depth: int = 0) -> dict[str, Any]:
        part_query = {
            **query,
            "params": {
                **query["params"],
                "start": str(epoch_nanoseconds(part.start)),
                "end": str(epoch_nanoseconds(part.end)),
            },
        }
        try:
            result = collect_loki(base_url, part, scenario, part_query, limit=100000)
            segments.append({"start": iso_utc(part.start), "end": iso_utc(part.end),
                             "records": result["query"]["records_returned"]})
            return result
        except Exception as exc:
            message = str(exc).lower()
            retryable = any(term in message for term in (
                "timed out", "remote end closed connection", "incompleteread",
                "connection reset", "connection aborted",
            ))
            if not retryable or depth >= 12:
                raise
            duration = part.end - part.start
            if duration <= timedelta(microseconds=1):
                raise
            midpoint = part.start + duration / 2
            left_end = midpoint
            # Keep the midpoint in both ranges. Loki bounds are inclusive; the
            # merge below deduplicates a boundary record by stream/timestamp/line.
            right_start = midpoint
            if left_end <= part.start or right_start >= part.end:
                raise
            left = Window(part.start, left_end, part.start, left_end, 0.0, 0.0)
            right = Window(right_start, part.end, right_start, part.end, 0.0, 0.0)
            return merge_loki_results(collect(left, depth + 1), collect(right, depth + 1),
                                      window, scenario, query, segments)

    # Loki can spend a long time evaluating a multi-minute query over all SUT
    # streams. Start with bounded, overlapping 15-second slices instead of
    # waiting for the full-run query to time out. Overlap is deduplicated below.
    parts: list[Window] = []
    cursor = window.start
    segment_width = timedelta(seconds=GLOBAL_LOKI_SEGMENT_SECONDS)
    while cursor < window.end:
        segment_end = min(cursor + segment_width, window.end)
        parts.append(Window(cursor, segment_end, cursor, segment_end, 0.0, 0.0))
        if segment_end >= window.end:
            break
        cursor = segment_end
    result: dict[str, Any] | None = None
    for part in parts:
        document = collect(part)
        result = document if result is None else merge_loki_results(result, document, window, scenario, query, segments)
    if result is None:
        raise DatasetError("global Loki window contains no queryable time range")
    # The recursive branch merges each split as it unwinds. A no-timeout query
    # still gets the global-window provenance required by the results package.
    result["query"]["window"] = {"start": iso_utc(window.start), "end": iso_utc(window.end)}
    result["query"]["params"] = query["params"]
    result["query"]["segments"] = segments
    result["query"]["records_returned"] = count_logs(result)
    return result


def merge_loki_results(left: dict[str, Any], right: dict[str, Any], window: Window,
                       scenario: str, query: dict[str, Any], segments: list[dict[str, Any]]) -> dict[str, Any]:
    streams: dict[str, dict[str, Any]] = {}
    all_pages: list[dict[str, Any]] = []
    page_metadata: list[dict[str, Any]] = []
    for document in (left, right):
        all_pages.extend(document.get("pages", []))
        page_metadata.extend(document.get("query", {}).get("pages", []))
        for stream in document.get("response", {}).get("data", {}).get("result", []):
            labels = json.dumps(stream.get("stream", {}), sort_keys=True, separators=(",", ":"))
            bucket = streams.setdefault(labels, {"stream": stream.get("stream", {}), "values": []})
            bucket["values"].extend(stream.get("values", []))
    merged = []
    for key in sorted(streams):
        stream = streams[key]
        unique = {(str(value[0]), str(value[1])): value for value in stream["values"]}
        stream["values"] = [unique[item] for item in sorted(unique, key=lambda pair: (int(pair[0]), pair[1]))]
        merged.append(stream)
    total = sum(len(stream["values"]) for stream in merged)
    return {
        "schema_version": "1.0", "scenario": scenario, "source": "loki",
        "query": {
            "backend": "loki", "scenario": scenario, "method": "GET", "path": query["path"],
            "params": query["params"], "pages": page_metadata,
            "window": {"start": iso_utc(window.start), "end": iso_utc(window.end)},
            "http_status": None, "records_returned": total, "page_count": len(all_pages),
            "segments": segments,
        },
        "response": {"status": "success", "data": {"resultType": "streams", "result": merged}},
        "pages": all_pages,
    }


def collect_global_jaeger(base_url: str, window: Window, scenario: str, query: dict[str, Any]) -> dict[str, Any]:
    """Search Jaeger in bounded time slices and union full traces by trace/span ID."""
    collection_started_at = utc_now()
    parts: list[Window] = []
    cursor = window.start
    segment_width = timedelta(seconds=GLOBAL_JAEGER_SEGMENT_SECONDS)
    while cursor < window.end:
        segment_end = min(cursor + segment_width, window.end)
        parts.append(Window(cursor, segment_end, cursor, segment_end, 0.0, 0.0))
        if segment_end >= window.end:
            break
        cursor = segment_end
    documents: list[dict[str, Any]] = []

    def collect(part: Window, depth: int = 0) -> None:
        try:
            documents.append(collect_jaeger(base_url, part, scenario, query, limit=10000))
        except Exception as exc:
            message = str(exc).lower()
            retryable = any(term in message for term in (
                "timed out", "remote end closed connection", "incompleteread",
                "connection reset", "connection aborted",
            ))
            if not retryable or depth >= 12 or part.end - part.start <= timedelta(microseconds=1):
                raise
            midpoint = part.start + (part.end - part.start) / 2
            left = Window(part.start, midpoint, part.start, midpoint, 0.0, 0.0)
            right = Window(midpoint, part.end, midpoint, part.end, 0.0, 0.0)
            collect(left, depth + 1)
            collect(right, depth + 1)

    for part in parts:
        collect(part)

    traces: dict[str, dict[str, Any]] = {}
    searches: list[dict[str, Any]] = []
    segments: list[dict[str, Any]] = []
    available_services: set[str] = set()
    occurrences = 0

    def canonical_process_ids(trace: dict[str, Any]) -> dict[str, Any]:
        """Namespace Jaeger process IDs by process metadata before cross-search union.

        Jaeger assigns short IDs such as ``p1`` independently in each search
        response. A later response can reuse ``p1`` for another service. Remap
        each payload's IDs to stable IDs derived from its complete process
        metadata so merging search results cannot silently change span owners.
        """
        value = json.loads(json.dumps(trace))
        remapped: dict[str, dict[str, Any]] = {}
        process_ids: dict[str, str] = {}
        for old_id, process in value.get("processes", {}).items():
            encoded = json.dumps(process, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            new_id = "process-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
            process_ids[old_id] = new_id
            remapped[new_id] = process
        for span in value.get("spans", []):
            old_id = span.get("processID")
            if old_id in process_ids:
                span["processID"] = process_ids[old_id]
        value["processes"] = remapped
        return value

    for document in documents:
        available_services.update(document["query"]["service_discovery"].get("sut_services_available", []))
        segments.append({"window": document["query"]["window"],
                         "service_discovery": document["query"]["service_discovery"],
                         "searches": document["query"]["searches"]})
        searches.extend(document["query"]["searches"])
        for result in document.get("search_results", []):
            for trace in result.get("response", {}).get("data", []):
                trace_id = trace.get("traceID")
                if not trace_id:
                    continue
                occurrences += 1
                normalized_trace = canonical_process_ids(trace)
                if trace_id not in traces:
                    traces[trace_id] = normalized_trace
                    continue
                target = traces[trace_id]
                known_spans = {span.get("spanID") for span in target.get("spans", [])}
                target.setdefault("spans", []).extend(
                    span for span in normalized_trace.get("spans", [])
                    if span.get("spanID") not in known_spans
                )
                target.setdefault("processes", {}).update(normalized_trace.get("processes", {}))
                target["spans"].sort(key=lambda span: (span.get("startTime", 0), span.get("spanID", "")))
    unique = [traces[key] for key in sorted(traces)]
    span_count = sum(len(trace.get("spans", [])) for trace in unique)
    return {
        "schema_version": "1.0", "scenario": scenario, "source": "jaeger",
        "query": {
            "backend": "jaeger", "scenario": scenario,
            "service_discovery": {"path": "/api/services", "http_status": 200,
                                  "sut_services_available": sorted(available_services)},
            "searches": searches, "segments": segments,
            "window": {"start": iso_utc(window.start), "end": iso_utc(window.end)},
            "records_returned_before_deduplication": occurrences,
            "unique_traces_returned": len(unique), "unique_spans_returned": span_count,
            "collection_started_at": collection_started_at,
            "collection_finished_at": utc_now(),
            "time_semantics": "Jaeger search start/end are Unix microseconds; all full traces returned in the global window are unioned by traceID and spanID.",
        },
        "search_results": [{"service": "global-sut-union", "response": {"data": unique}}],
    }


def source_for_run(validation_id: str, sequence: int, fault: str) -> tuple[Path, str]:
    child_id = f"{validation_id}-{sequence:03d}"
    package_root = ARTIFACT_ROOT / child_id
    inner_id = f"edv-{child_id}-{fault.lower()}"
    run_id = run_id_for(inner_id, fault)
    return package_root / fault, run_id


def publish_fault_run(results_root: Path, run: dict[str, Any], source_root: Path, expected_run_id: str) -> dict[str, Any]:
    gt_path = source_root / "02-ground-truth" / "ground-truth.json"
    dataset_paths = {
        "A": source_root / "01-telemetry/scenario-a/rca-window/dataset.json",
        "B": source_root / "01-telemetry/scenario-b/rca-window/dataset.json",
    }
    if not gt_path.is_file() or any(not path.is_file() for path in dataset_paths.values()):
        raise ResultsError(f"fault source package incomplete for sequence {run['sequence']}: {source_root}")
    gt = read_json(gt_path)
    datasets = {scenario: read_json(path) for scenario, path in dataset_paths.items()}
    run_ids = {gt.get("run_id"), *(item.get("run_id") for item in datasets.values())}
    if run_ids != {expected_run_id}:
        raise ResultsError(f"run identity mismatch for {run['directory']}: expected {expected_run_id}, found {sorted(str(x) for x in run_ids)}")
    if gt.get("status") != "valid" or not gt.get("validation", {}).get("injection_confirmed") or not gt.get("validation", {}).get("recovery_confirmed"):
        raise ResultsError(f"Ground Truth is not valid and recovered for {run['directory']}")
    target = results_root / run["directory"]
    if target.exists():
        raise ResultsError(f"refusing to overwrite result folder: {target}")
    target.mkdir()
    shutil.copy2(dataset_paths["A"], target / "datasetA.json")
    shutil.copy2(dataset_paths["B"], target / "datasetB.json")
    shutil.copy2(gt_path, target / "ground-truth.json")
    if any(sha256_file(src) != sha256_file(target / name) for src, name in (
        (dataset_paths["A"], "datasetA.json"), (dataset_paths["B"], "datasetB.json"),
        (gt_path, "ground-truth.json"),
    )):
        raise ResultsError(f"copy hash mismatch for {run['directory']}")
    workload_window = workload_event_window(source_root)
    return {**run, "run_id": expected_run_id, "workload_window": workload_window,
            "files": {name: file_info(target / name) for name in ("datasetA.json", "datasetB.json", "ground-truth.json")}}


def workload_event_window(source_root: Path) -> dict[str, str]:
    events_path = source_root / "audit/run-control/orchestrator-events.jsonl"
    if not events_path.is_file():
        raise ResultsError(f"run-control events missing: {events_path}")
    events = read_jsonl(events_path)
    starts = [event["timestamp"] for event in events if event.get("event") == "warmup_started"]
    ends = [event["timestamp"] for event in events if event.get("event") == "workload_finished"]
    if not starts or not ends:
        raise ResultsError(f"run-control events lack workload boundaries: {events_path}")
    start = min((parse_timestamp(value, "warmup_started") for value in starts))
    end = max((parse_timestamp(value, "workload_finished") for value in ends))
    if end <= start:
        raise ResultsError(f"invalid workload event window in {events_path}")
    return {"start": iso_utc(start), "end": iso_utc(end)}


def collect_global_telemetry(results_root: Path, experiment_start: str, experiment_end: str) -> dict[str, Any]:
    window = global_capture_window(experiment_start, experiment_end)
    plan = build_query_plan(window)
    results: dict[str, Any] = {}
    with PortForwardSet() as urls:
        for scenario in SCENARIOS:
            queries = {item["backend"]: item for item in plan[scenario]}
            documents = {
                "metrics": collect_prometheus(urls[f"{scenario}.prometheus"], window, scenario, queries["prometheus"]),
                "logs": collect_global_loki(urls[f"{scenario}.loki"], window, scenario, queries["loki"]),
                "traces": collect_global_jaeger(urls[f"{scenario}.jaeger"], window, scenario, queries["jaeger"]),
            }
            if unique_traces_and_spans(documents["traces"])[0] == 0:
                raise ResultsError(f"Jaeger returned zero SUT traces for scenario {scenario} in a workload window; refusing an incomplete global snapshot")
            scenario_dir = results_root / f"dataset{scenario}"
            scenario_dir.mkdir(exist_ok=False)
            if set(scenario_dir.iterdir()):
                raise ResultsError(f"global dataset folder was not empty: {scenario_dir}")
            counts: dict[str, Any] = {}
            for signal, document in documents.items():
                path = scenario_dir / f"{signal}.json"
                write_json(path, document)
                if signal == "metrics":
                    series, samples = count_metrics(document)
                    counts[signal] = {"series": series, "samples": samples}
                elif signal == "logs":
                    counts[signal] = {"records": count_logs(document)}
                else:
                    traces, spans = unique_traces_and_spans(document)
                    occurrences = document.get("query", {}).get("records_returned_before_deduplication", traces)
                    counts[signal] = {"trace_search_occurrences": occurrences, "unique_traces": traces, "unique_spans": spans}
            results[scenario] = {"directory": f"dataset{scenario}", "counts": counts,
                                 "files": {signal: file_info(scenario_dir / f"{signal}.json") for signal in documents},
                                 "observed_timestamps": {signal: telemetry_timestamps(documents[signal]) for signal in documents}}
    return {"window": {"experiment_start": iso_utc(window.start), "experiment_end": iso_utc(window.end)},
            "scenarios": results}


def finish_global_capture(results_root: Path, validation_id: str, campaign: list[dict[str, Any]],
                          experiment_start: str, experiment_end: str) -> dict[str, Any]:
    expected_root = expected_results_path(REPO)
    if results_root.resolve() != expected_root:
        raise ResultsError(f"refusing to capture outside latest result root: {results_root}")
    # A prior failed collection may have left one scenario partially written.
    # Remove only the two fixed global telemetry directories for this same run.
    for name in ("datasetA", "datasetB"):
        path = results_root / name
        if path.is_symlink():
            raise ResultsError(f"refusing to replace symlink in results root: {path}")
        if path.exists():
            if not path.is_dir() or path.resolve() != (expected_root / name):
                raise ResultsError(f"refusing to replace unexpected global dataset path: {path}")
            shutil.rmtree(path)
    collection_started_at = utc_now()
    global_telemetry = collect_global_telemetry(results_root, experiment_start, experiment_end)
    collection_finished_at = utc_now()
    manifest = {
        "schema_version": "1.0", "validation_id": validation_id, "status": "complete",
        "experiment_start": global_telemetry["window"]["experiment_start"],
        "experiment_end": global_telemetry["window"]["experiment_end"],
        "collection_started_at": collection_started_at, "collection_finished_at": collection_finished_at,
        "fault_runs": campaign,
        "global_telemetry": global_telemetry,
        "invariants": {"global_raw_queried_once_per_signal_per_scenario": True,
                       "fault_datasets_and_ground_truth_share_run_id": True,
                       "datasetA_and_datasetB_have_no_subdirectories": True,
                       "cleanup_scope": str(expected_results_path(REPO))},
    }
    write_json(results_root / "manifest.json", manifest)
    (results_root / "README.md").write_text(readme_text(manifest), encoding="utf-8")
    validate_result_layout(results_root, manifest)
    return manifest


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if not args.resume_global and not (args.all or args.fault):
            raise ResultsError("select --all, at least one --fault, or --resume-global")
        faults = () if args.resume_global else selected_faults(args)
        validation_id = validate_validation_id(args.validation_id, max(len(faults), 5 if args.resume_global else 1))
        if args.resume_global:
            if args.dry_run:
                raise ResultsError("--dry-run is not available with --resume-global")
            results_root = expected_results_path(REPO)
            progress = read_json(results_root / "manifest.json")
            if progress.get("validation_id") != validation_id or progress.get("status") != "in_progress":
                raise ResultsError("resume requires a matching in-progress audit/results/manifest.json")
            campaign = progress.get("fault_runs")
            if not isinstance(campaign, list) or not campaign:
                raise ResultsError("in-progress manifest has no completed fault runs")
            expected_dirs = {item["directory"] for item in campaign}
            for item in campaign:
                fault_dir = results_root / item["directory"]
                expected_files = {"datasetA.json", "datasetB.json", "ground-truth.json"}
                if not fault_dir.is_dir() or {path.name for path in fault_dir.iterdir()} != expected_files:
                    raise ResultsError(f"cannot resume: fault snapshot is incomplete: {fault_dir}")
                gt = read_json(fault_dir / "ground-truth.json")
                datasets = [read_json(fault_dir / f"dataset{scenario}.json") for scenario in ("A", "B")]
                if gt.get("run_id") != item.get("run_id") or any(dataset.get("run_id") != item.get("run_id") for dataset in datasets):
                    raise ResultsError(f"cannot resume: mixed physical run in {fault_dir}")
            actual_dirs = {path.name for path in results_root.iterdir() if path.is_dir() and path.name not in {"datasetA", "datasetB"}}
            if actual_dirs != expected_dirs:
                raise ResultsError("cannot resume: fault directory set differs from in-progress manifest")
            start = min((parse_timestamp(item["workload_window"]["start"], "workload_start") for item in campaign))
            end = max((parse_timestamp(item["workload_window"]["end"], "workload_end") for item in campaign))
            ready = subprocess.run(["kubectl", "get", "nodes", "-o", "json"], cwd=REPO, capture_output=True, text=True)
            if ready.returncode != 0:
                raise ResultsError(f"Kubernetes preflight failed: {ready.stderr.strip()}")
            manifest = finish_global_capture(results_root, validation_id, campaign, iso_utc(start), iso_utc(end))
            print(f"results_root={results_root.relative_to(REPO)}")
            print(f"fault_runs={len(campaign)}")
            print(f"experiment_window={manifest['experiment_start']} -> {manifest['experiment_end']}")
            return 0
        runs = fault_run_names(faults)
        if args.pre_buffer < 0 or args.post_buffer < 0:
            raise ResultsError("pre/post buffers must be non-negative")
        planned = []
        for run in runs:
            source, run_id = source_for_run(validation_id, run["sequence"], run["fault"])
            planned.append({**run, "validation_id": f"{validation_id}-{run['sequence']:03d}",
                            "run_id": run_id, "result_directory": run["directory"],
                            "package_source": source.relative_to(REPO).as_posix()})
        if args.dry_run:
            print(json.dumps({"results_root": "audit/results", "will_clean_exactly": str(expected_results_path(REPO)),
                              "fault_runs": planned, "pre_buffer_seconds": args.pre_buffer,
                              "post_buffer_seconds": args.post_buffer}, indent=2))
            return 0

        # Check fresh child IDs before deleting the previous latest-results snapshot.
        occupied = []
        for item in planned:
            source_root = REPO / item["package_source"]
            if source_root.exists():
                occupied.append(str(source_root))
            if (REPO / "artifacts/experiments" / item["run_id"]).exists():
                occupied.append(str(REPO / "artifacts/experiments" / item["run_id"]))
        if occupied:
            raise ResultsError(f"run IDs are not fresh; refusing to replace latest results: {occupied}")
        # Fail fast on cluster readiness before removing the current output.
        ready = subprocess.run(["kubectl", "get", "nodes", "-o", "json"], cwd=REPO, capture_output=True, text=True)
        if ready.returncode != 0:
            raise ResultsError(f"Kubernetes preflight failed: {ready.stderr.strip()}")
        nodes = json.loads(ready.stdout).get("items", [])
        if not nodes or any(not any(c.get("type") == "Ready" and c.get("status") == "True" for c in node.get("status", {}).get("conditions", [])) for node in nodes):
            raise ResultsError("Kubernetes preflight found a node that is not Ready")

        results_root = clean_results_only(REPO)
        (results_root / "README.md").write_text(readme_text(), encoding="utf-8")
        campaign: list[dict[str, Any]] = []
        for item in planned:
            command = [sys.executable, "scripts/experiment/experiment_data_validation.py",
                       "--validation-id", item["validation_id"], "--fault", item["fault"].lower(),
                       "--pre-buffer", str(args.pre_buffer), "--post-buffer", str(args.post_buffer)]
            child = subprocess.run(command, cwd=REPO, check=False)
            if child.returncode:
                raise ResultsError(f"fault run {item['directory']} failed with exit {child.returncode}; partial latest output retained at audit/results")
            source_root = REPO / item["package_source"]
            campaign.append(publish_fault_run(results_root, item, source_root, item["run_id"]))
            current_experiment_start = campaign[0]["workload_window"]["start"]
            write_json(results_root / "manifest.json", {
                "schema_version": "1.0", "validation_id": validation_id,
                "experiment_start": current_experiment_start, "status": "in_progress", "fault_runs": campaign,
            })

        experiment_start = campaign[0]["workload_window"]["start"]
        experiment_end = max((item["workload_window"]["end"] for item in campaign), key=lambda value: parse_timestamp(value, "workload_end"))
        manifest = finish_global_capture(results_root, validation_id, campaign, experiment_start, experiment_end)
        print(f"results_root={results_root.relative_to(REPO)}")
        print(f"fault_runs={len(campaign)}")
        print(f"experiment_window={manifest['experiment_start']} -> {manifest['experiment_end']}")
        return 0
    except Exception as exc:
        print(f"results battery: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


def readme_text(manifest: dict[str, Any] | None = None) -> str:
    intro = """# Latest experiment results\n\n`datasetA/` and `datasetB/` contain the unnormalized backend captures for all SUT services over the complete experiment window. Each contains exactly `metrics.json`, `logs.json`, and `traces.json`. This is one global snapshot after the final fault, not a concatenation of per-fault captures. Loki and Jaeger requests are partitioned into bounded time segments; transient failures trigger further subdivision, and inclusive boundaries are deduplicated.\n\nEach numbered fault-run directory contains the Phase 5 normalized RCA-window datasets from that physical run (`datasetA.json`, `datasetB.json`) and its byte-preserved original Ground Truth (`ground-truth.json`).\n\nThe global raw telemetry is an experimental source snapshot. It is not automatically the RCA input. Per-run RCA inputs remain generated by the Phase 6 pipeline. The result root is cleared and recreated before each new battery and contains only the latest attempt.\n"""
    if not manifest:
        return intro
    return intro + f"\nExperiment window (UTC): `{manifest['experiment_start']}` → `{manifest['experiment_end']}`.\n"


def validate_result_layout(results_root: Path, manifest: dict[str, Any]) -> None:
    expected_root = {"README.md", "manifest.json", "datasetA", "datasetB"} | {item["directory"] for item in manifest["fault_runs"]}
    actual_root = {path.name for path in results_root.iterdir()}
    if actual_root != expected_root:
        raise ResultsError(f"unexpected top-level results entries: expected {sorted(expected_root)}, got {sorted(actual_root)}")
    for scenario in ("datasetA", "datasetB"):
        folder = results_root / scenario
        expected = {"metrics.json", "logs.json", "traces.json"}
        actual = {path.name for path in folder.iterdir()}
        if actual != expected or any(path.is_dir() for path in folder.iterdir()):
            raise ResultsError(f"{scenario} must contain exactly {sorted(expected)} and no subdirectories; got {sorted(actual)}")
    if manifest["experiment_end"] <= manifest["experiment_start"]:
        raise ResultsError("invalid global experiment window")
    expected_fault_dirs = {item["directory"] for item in manifest["fault_runs"]}
    actual_fault_dirs = {path.name for path in results_root.iterdir() if path.is_dir() and path.name not in ("datasetA", "datasetB")}
    if actual_fault_dirs != expected_fault_dirs:
        raise ResultsError(f"fault directory set mismatch: expected {sorted(expected_fault_dirs)}, got {sorted(actual_fault_dirs)}")
    for item in manifest["fault_runs"]:
        directory = results_root / item["directory"]
        expected_files = {"datasetA.json", "datasetB.json", "ground-truth.json"}
        if {path.name for path in directory.iterdir()} != expected_files:
            raise ResultsError(f"fault result folder has unexpected files: {directory}")
        gt = read_json(directory / "ground-truth.json")
        d_a = read_json(directory / "datasetA.json")
        d_b = read_json(directory / "datasetB.json")
        if {gt.get("run_id"), d_a.get("run_id"), d_b.get("run_id")} != {item["run_id"]}:
            raise ResultsError(f"A/B/GT do not belong to the same run: {directory.name}")
    for scenario in ("A", "B"):
        for signal, recorded in manifest["global_telemetry"]["scenarios"][scenario]["files"].items():
            path = results_root / f"dataset{scenario}" / f"{signal}.json"
            if sha256_file(path) != recorded["sha256"]:
                raise ResultsError(f"global raw hash mismatch: {path}")
            document = read_json(path)
            query_window = document.get("query", {}).get("window", {})
            if query_window.get("start") != manifest["experiment_start"] or query_window.get("end") != manifest["experiment_end"]:
                raise ResultsError(f"global {scenario} {signal} query did not use the whole experiment window")


def telemetry_timestamps(document: dict[str, Any]) -> dict[str, str | None]:
    source = document.get("source")
    timestamps: list[datetime] = []
    if source == "prometheus":
        for series in document.get("response", {}).get("data", {}).get("result", []):
            points = series.get("values", [])
            if not points and series.get("value"):
                points = [series["value"]]
            for point in points:
                try:
                    timestamps.append(datetime.fromtimestamp(float(point[0]), timezone.utc))
                except (TypeError, ValueError, OSError, OverflowError):
                    continue
    elif source == "loki":
        for stream in document.get("response", {}).get("data", {}).get("result", []):
            for point in stream.get("values", []):
                try:
                    timestamps.append(datetime.fromtimestamp(int(point[0]) / 1_000_000_000, timezone.utc))
                except (TypeError, ValueError, OSError, OverflowError):
                    continue
    elif source == "jaeger":
        seen: set[tuple[str, str]] = set()
        for result in document.get("search_results", []):
            for trace in result.get("response", {}).get("data", []):
                trace_id = str(trace.get("traceID", ""))
                for span in trace.get("spans", []):
                    span_id = str(span.get("spanID", ""))
                    key = (trace_id, span_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    try:
                        start = int(span["startTime"])
                        duration = int(span.get("duration", 0))
                        timestamps.append(datetime.fromtimestamp(start / 1_000_000, timezone.utc))
                        timestamps.append(datetime.fromtimestamp((start + duration) / 1_000_000, timezone.utc))
                    except (KeyError, TypeError, ValueError, OSError, OverflowError):
                        continue
    if not timestamps:
        return {"first": None, "last": None}
    return {"first": iso_utc(min(timestamps)), "last": iso_utc(max(timestamps))}


if __name__ == "__main__":
    raise SystemExit(main())
