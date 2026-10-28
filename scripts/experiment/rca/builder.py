from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from ..dataset.core import DatasetError, SUT_SERVICES, canonical_json, read_json, sha256_file, write_json_atomic
from . import __version__


HERE = Path(__file__).resolve().parent
PROMPT_PATH = HERE / "prompt.txt"
REQUEST_SCHEMA_PATH = HERE / "rca-request.schema.json"
OUTPUT_SCHEMA_PATH = HERE / "rca-output.schema.json"
FAULT_TYPES = ("cpu_saturation", "network_latency", "service_unavailable", "application_exception", "datastore_unavailable")
LOCATION_KINDS = ("service", "service_edge", "operation", "datastore")


class RCAInputError(RuntimeError):
    pass


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_static() -> tuple[str, dict[str, Any], dict[str, Any]]:
    try:
        prompt = PROMPT_PATH.read_text(encoding="utf-8").strip()
        request_schema = read_json(REQUEST_SCHEMA_PATH)
        output_schema = read_json(OUTPUT_SCHEMA_PATH)
        Draft202012Validator.check_schema(request_schema)
        Draft202012Validator.check_schema(output_schema)
    except (OSError, DatasetError, ValueError) as exc:
        raise RCAInputError(f"invalid static RCA contract file: {exc}") from exc
    return prompt, request_schema, output_schema


def _validate_dataset(dataset: Any, scenario: str, run_id: str) -> None:
    if not isinstance(dataset, dict):
        raise RCAInputError("source dataset must be a JSON object")
    if not isinstance(run_id, str) or not run_id or scenario not in ("A", "B"):
        raise RCAInputError("source dataset is missing a valid run/scenario identity")
    if dataset.get("schema_version") != "1.0":
        raise RCAInputError("unsupported dataset schema_version")
    if dataset.get("scenario") != scenario or dataset.get("run_id") != run_id:
        raise RCAInputError("dataset run/scenario does not match the requested control-plane location")
    if set(dataset.get("services", [])) != set(SUT_SERVICES):
        raise RCAInputError("dataset component scope differs from the fixed SUT catalog")
    for signal in ("metrics", "logs", "traces"):
        if not isinstance(dataset.get(signal), list):
            raise RCAInputError(f"dataset {signal} must be an array")
    for key in ("start", "end"):
        if not isinstance(dataset.get("window", {}).get(key), str):
            raise RCAInputError("dataset observation window is incomplete")


def build_request(dataset: dict[str, Any]) -> dict[str, Any]:
    """Build model-visible content from one normalized dataset only."""
    scenario = dataset.get("scenario")
    run_id = dataset.get("run_id")
    _validate_dataset(dataset, scenario, run_id)
    prompt, _, output_schema = _load_static()
    request = {
        "schema_version": "1.0",
        "task": "root_cause_analysis",
        "instructions": prompt,
        "system_context": {"architecture": "microservices", "components": list(SUT_SERVICES)},
        "controlled_vocabulary": {"fault_types": list(FAULT_TYPES), "location_kinds": list(LOCATION_KINDS)},
        "output_contract": output_schema,
        "observation_window": {"start": dataset["window"]["start"], "end": dataset["window"]["end"]},
        "telemetry": {signal: dataset[signal] for signal in ("metrics", "logs", "traces")},
    }
    validate_request(request)
    if run_id in _serialize_json(request).decode("utf-8"):
        raise RCAInputError("run_id value leaked into model-visible request")
    return request


def validate_request(request: dict[str, Any]) -> None:
    _, request_schema, _ = _load_static()
    errors = sorted(Draft202012Validator(request_schema, format_checker=FormatChecker()).iter_errors(request), key=lambda e: list(e.absolute_path))
    if errors:
        err = errors[0]
        path = ".".join(map(str, err.absolute_path)) or "<root>"
        raise RCAInputError(f"request schema violation at {path}: {err.message}")


def _all_evidence_ids(request: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    telemetry = request["telemetry"]
    for signal in ("metrics", "logs", "traces"):
        for record in telemetry[signal]:
            if isinstance(record.get("evidence_id"), str):
                found.add(record["evidence_id"])
            if signal == "traces":
                for span in record.get("spans", []):
                    if isinstance(span.get("evidence_id"), str):
                        found.add(span["evidence_id"])
    return found


def validate_response(response: Any, request: dict[str, Any]) -> None:
    """Reusable contract/evidence validation; does not score RCA quality."""
    _, _, output_schema = _load_static()
    errors = sorted(Draft202012Validator(output_schema, format_checker=FormatChecker()).iter_errors(response), key=lambda e: list(e.absolute_path))
    if errors:
        err = errors[0]
        path = ".".join(map(str, err.absolute_path)) or "<root>"
        raise RCAInputError(f"response schema violation at {path}: {err.message}")
    hypotheses = response["hypotheses"]
    if [item["rank"] for item in hypotheses] != [1, 2, 3]:
        raise RCAInputError("hypothesis ranks must be exactly ordered 1, 2, 3")
    signatures = [(item["fault_type"], canonical_json(item["location"])) for item in hypotheses]
    if len(set(signatures)) != 3:
        raise RCAInputError("hypotheses must have distinct fault type/location pairs")
    valid_ids = _all_evidence_ids(request)
    for hypothesis in hypotheses:
        unknown = set(hypothesis["evidence"]) - valid_ids
        if unknown:
            raise RCAInputError(f"response cites evidence IDs absent from request: {sorted(unknown)}")


def _serialize_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def render_preview(request: dict[str, Any]) -> bytes:
    """Render only the request content, with no control-plane annotations."""
    text = "# Canonical Full RCA Request\n\n```json\n" + json.dumps(
        request, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False
    ) + "\n```\n"
    return text.encode("utf-8")


def _builder_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=HERE.parents[3], check=True,
                              capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _request_counts(request: dict[str, Any]) -> dict[str, int]:
    telemetry = request["telemetry"]
    return {
        "metrics_count": len(telemetry["metrics"]),
        "logs_count": len(telemetry["logs"]),
        "traces_count": len(telemetry["traces"]),
        "spans_count": sum(len(trace.get("spans", [])) for trace in telemetry["traces"]),
    }


def _source_provenance_markers(request: dict[str, Any]) -> dict[str, int]:
    """Count native treatment/provenance metadata without rewriting source telemetry."""
    counts: dict[str, int] = {}
    watched_keys = {"telemetry_scenario", "telemetry.scenario", "telemetry_sdk_name", "otel.scope.name", "otel_scope_name"}
    watched_values = {"conventional", "otel", "opentelemetry"}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                key_text = str(key)
                if key_text.lower() in watched_keys:
                    counts[f"key:{key_text}"] = counts.get(f"key:{key_text}", 0) + 1
                if isinstance(child, str) and child.lower() in watched_values:
                    counts[f"value:{child}"] = counts.get(f"value:{child}", 0) + 1
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(request["telemetry"])
    return dict(sorted(counts.items()))


def generate_one(dataset_path: Path, output_path: Path, run_id: str, scenario: str, replace: bool = False) -> dict[str, Any]:
    if output_path.exists():
        if not replace:
            raise RCAInputError(f"output already exists; refusing to overwrite: {output_path}")
        import shutil
        shutil.rmtree(output_path)
    dataset = read_json(dataset_path)
    _validate_dataset(dataset, scenario, run_id)
    request = build_request(dataset)
    request_bytes = _serialize_json(request)
    preview_bytes = render_preview(request)
    prompt, request_schema, output_schema = _load_static()
    output_path.mkdir(parents=True, exist_ok=False)
    (output_path / "request.json").write_bytes(request_bytes)
    (output_path / "request-preview.md").write_bytes(preview_bytes)
    try:
        repo_root = HERE.parents[3]
        source_label = dataset_path.resolve().relative_to(repo_root).as_posix()
    except ValueError:
        source_label = dataset_path.name
    manifest = {
        "schema_version": "1.0",
        "run_id": run_id,
        "scenario": scenario,
        "source_dataset_path": source_label,
        "source_dataset_sha256": sha256_file(dataset_path),
        "request_sha256": _sha256_bytes(request_bytes),
        "preview_sha256": _sha256_bytes(preview_bytes),
        "prompt_sha256": _sha256_bytes(prompt.encode("utf-8")),
        "request_schema_sha256": sha256_file(REQUEST_SCHEMA_PATH),
        "output_schema_sha256": sha256_file(OUTPUT_SCHEMA_PATH),
        "builder_version": __version__,
        "builder_commit": _builder_commit(),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "request_bytes": len(request_bytes),
        "preview_bytes": len(preview_bytes),
        "content_policy_checks": {
            "run_id_absent_from_request": run_id not in request_bytes.decode("utf-8"),
            "dataset_scenario_field_not_copied_to_request_envelope": "scenario" not in request,
            "ground_truth_and_control_plane_not_loaded": True,
            "source_telemetry_records_preserved": True,
        },
        "native_telemetry_provenance_markers": _source_provenance_markers(request),
        **_request_counts(request),
    }
    write_json_atomic(output_path / "manifest.json", manifest)
    return manifest


def generate_run(repo_root: Path, run_id: str, scenarios: tuple[str, ...] = ("A", "B"), replace: bool = False) -> dict[str, dict[str, Any]]:
    if not run_id or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in run_id):
        raise RCAInputError("run_id must be lowercase DNS-safe text")
    dataset_root = repo_root / "artifacts" / "datasets" / run_id
    output_root = repo_root / "artifacts" / "rca-inputs" / run_id
    manifests: dict[str, dict[str, Any]] = {}
    for scenario in scenarios:
        if scenario not in ("A", "B"):
            raise RCAInputError(f"unsupported scenario: {scenario}")
        suffix = "a" if scenario == "A" else "b"
        manifests[scenario] = generate_one(
            dataset_root / f"scenario-{suffix}" / "dataset.json",
            output_root / f"scenario-{suffix}", run_id, scenario, replace=replace,
        )
    if set(scenarios) == {"A", "B"}:
        request_a = read_json(output_root / "scenario-a" / "request.json")
        request_b = read_json(output_root / "scenario-b" / "request.json")
        if {k: v for k, v in request_a.items() if k != "telemetry"} != {k: v for k, v in request_b.items() if k != "telemetry"}:
            raise RCAInputError("static canonical request contract differs between A and B")
    return manifests


def validate_pair(request_a: dict[str, Any], request_b: dict[str, Any]) -> bool:
    return ({k: v for k, v in request_a.items() if k != "telemetry"}
            == {k: v for k, v in request_b.items() if k != "telemetry"})
