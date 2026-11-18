"""Canonical, repetition-scalable result layout for a complete experiment run."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any, Iterable, Mapping


FAULTS = ("F1", "F2", "F3", "F4", "F5")
TREATMENTS = ("A", "B")
RUN_ITEMS = ("manifest.json", "ground-truth.json", "raw", "telemetry", "llm", "evaluation", "hashes.sha256")
INSTANCE_RE = re.compile(r"^(F[1-5])-R([0-9]{2,})$", re.ASCII)


class ResultLayoutError(ValueError):
    """Raised when a result run does not satisfy the canonical layout."""


def validate_repetitions(repetitions: int) -> int:
    if type(repetitions) is not int or repetitions < 1:
        raise ResultLayoutError("repetitions must be a positive integer")
    return repetitions


def repetition_plan(repetitions: int | Mapping[str, int]) -> dict[str, int]:
    if type(repetitions) is int:
        validate_repetitions(repetitions)
        return {fault: repetitions for fault in FAULTS}
    if not isinstance(repetitions, Mapping) or set(repetitions) - set(FAULTS):
        raise ResultLayoutError("fault repetitions must map known fault IDs to non-negative integers")
    plan = {fault: repetitions.get(fault, 0) for fault in FAULTS}
    if any(type(value) is not int or value < 0 for value in plan.values()):
        raise ResultLayoutError("fault repetitions must be non-negative integers")
    if not any(plan.values()):
        raise ResultLayoutError("at least one physical fault instance is required")
    return plan


def instance_ids(repetitions: int | Mapping[str, int]) -> list[str]:
    plan = repetition_plan(repetitions)
    return [f"{fault}-R{rep:02d}" for fault in FAULTS
            for rep in range(1, plan[fault] + 1)]


def case_id(instance_id: str, treatment: str) -> str:
    if not INSTANCE_RE.fullmatch(instance_id) or treatment not in TREATMENTS:
        raise ResultLayoutError("invalid physical instance or treatment")
    return f"{instance_id}-{treatment}"


def parse_case_id(value: str) -> tuple[str, str, int, str]:
    match = re.fullmatch(r"(F[1-5])-R([0-9]{2,})-([AB])", value, re.ASCII)
    if not match:
        raise ResultLayoutError(f"invalid case identity: {value!r}")
    fault, repetition, treatment = match.groups()
    return value, fault, int(repetition), treatment


def expected_case_ids(repetitions: int | Mapping[str, int]) -> list[str]:
    return [case_id(instance, treatment) for instance in instance_ids(repetitions)
            for treatment in TREATMENTS]


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode("utf-8")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_json_bytes(value))


def build_ground_truth(instances: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for identity in instance_ids_from_records(instances):
        record = instances[identity]["ground_truth"]
        _, fault, repetition, _ = parse_case_id(identity + "-A")
        key = f"R{repetition:02d}"
        if not isinstance(record, Mapping) or record.get("status") != "RECOVERED" \
                or record.get("execution_eligible") is not True:
            raise ResultLayoutError(f"ineligible Ground Truth for {identity}")
        result.setdefault(fault, {})[key] = dict(record)
    return result


def create_run_tree(destination: Path, run_id: str, repetitions: int | Mapping[str, int],
                    instances: Mapping[str, Mapping[str, Any]], global_raw: Path,
                    manifest_fields: Mapping[str, Any]) -> dict[str, Any]:
    """Create the scientific run tree from validated Builder/GT outputs.

    `instances[Fx-Ryy]` supplies `ground_truth`, `raw_bundle`, `datasets` (A/B
    dataset paths), and `inputs` (A/B RCA Input paths). The orchestrator owns
    the private input workspace; only exact model requests are published under
    `llm/inputs/`.
    """
    plan = repetition_plan(repetitions)
    identities = instance_ids(plan)
    if set(instances) != set(identities):
        raise ResultLayoutError("physical instance set does not match configured repetition grid")
    if destination.exists():
        raise ResultLayoutError("staged run destination already exists")
    destination.mkdir(parents=True)
    for name in ("telemetry", "llm/inputs", "llm/responses", "evaluation"):
        (destination / name).mkdir(parents=True)
    copy_raw(global_raw, destination / "raw")
    write_json(destination / "ground-truth.json", build_ground_truth(instances))
    for identity in identities:
        record = instances[identity]
        raw_bundle = Path(record["raw_bundle"])
        raw_manifest = json.loads((raw_bundle / "manifest.json").read_text(encoding="utf-8"))
        raw_hash_path = raw_bundle / "hashes.sha256"
        for treatment in TREATMENTS:
            case = case_id(identity, treatment)
            dataset_path = Path(record["datasets"][treatment])
            dataset_bytes = dataset_path.read_bytes()
            dataset = json.loads(dataset_bytes)
            source_manifest = json.loads((dataset_path.parent / "build-manifest.json").read_text(encoding="utf-8"))
            telemetry = {
                **dataset,
                "_provenance": {
                    "fault_instance": identity,
                    "treatment": treatment,
                    "source_window": raw_manifest.get("window"),
                    "raw_manifest_sha256": hashlib.sha256((raw_bundle / "manifest.json").read_bytes()).hexdigest(),
                    "raw_hashes_sha256": hashlib.sha256(raw_hash_path.read_bytes()).hexdigest(),
                    "builder_script_sha256": source_manifest.get("builder_script_sha256"),
                    "builder_output_sha256": hashlib.sha256(dataset_bytes).hexdigest(),
                },
            }
            write_json(destination / "telemetry" / f"{case}.json", telemetry)
            source_input = Path(record["inputs"][treatment])
            if not source_input.is_file():
                raise ResultLayoutError(f"missing private RCA Input source: {source_input}")
    manifest = {
        "run_id": run_id,
        "status": "STAGING",
        "repetitions_per_fault": (next(iter({plan[f] for f in FAULTS if plan[f] > 0}))
                                  if len({plan[f] for f in FAULTS if plan[f] > 0}) == 1 else None),
        "fault_repetitions": plan,
        "faults": [fault for fault in FAULTS if plan[fault] > 0],
        "physical_fault_instances": len(identities),
        "treatments": list(TREATMENTS),
        "rca_inputs": len(identities) * 2,
        "expected_treatment_cases": len(identities) * 2,
        **dict(manifest_fields),
    }
    write_json(destination / "manifest.json", manifest)
    return {"identities": identities, "case_ids": expected_case_ids(repetitions)}


def instance_ids_from_records(instances: Mapping[str, Any]) -> list[str]:
    identities = sorted(instances, key=lambda x: (int(x[1]), int(x.split("-R", 1)[1])))
    if not identities:
        raise ResultLayoutError("a run must contain physical fault instances")
    if any(not INSTANCE_RE.fullmatch(identity) for identity in identities):
        raise ResultLayoutError("invalid physical instance identity")
    # Every selected fault has a contiguous R01..Rn sequence; omitted faults are valid.
    by_fault: dict[str, set[int]] = {}
    for identity in identities:
        fault, rep = identity.split("-R", 1)
        by_fault.setdefault(fault, set()).add(int(rep))
    for fault, values in by_fault.items():
        if values != set(range(1, max(values) + 1)):
            raise ResultLayoutError(f"repetition sequence for {fault} must start at R01 without gaps")
    ordering = {fault: index for index, fault in enumerate(FAULTS)}
    identities.sort(key=lambda value: (ordering[value.split("-R", 1)[0]],
                                       int(value.split("-R", 1)[1])))
    return identities


def copy_raw(global_raw: Path, destination: Path) -> None:
    """Copy native global A/B responses into the published modality hierarchy."""
    for side, source_name in zip(TREATMENTS, ("datasetA", "datasetB")):
        source = global_raw / source_name
        if not source.is_dir():
            raise ResultLayoutError(f"missing global RAW treatment: {source_name}")
        target = destination / side
        metrics = source / "metrics.json"
        if not metrics.is_file():
            raise ResultLayoutError(f"missing native metrics response for {side}")
        (target / "metrics").mkdir(parents=True)
        shutil.copy2(metrics, target / "metrics" / metrics.name)
        for modality in ("logs", "traces"):
            modality_source = source / modality
            if not modality_source.is_dir():
                raise ResultLayoutError(f"missing native {modality} responses for {side}")
            shutil.copytree(modality_source, target / modality, copy_function=shutil.copy2)


def outer_hashes(root: Path) -> list[tuple[str, str]]:
    root_manifest = root / "hashes.sha256"
    files = sorted((path for path in root.rglob("*") if path.is_file() and path != root_manifest),
                   key=lambda path: path.relative_to(root).as_posix())
    entries = [(hashlib.sha256(path.read_bytes()).hexdigest(), path.relative_to(root).as_posix())
               for path in files]
    (root / "hashes.sha256").write_text("".join(f"{digest}  {rel}\n" for digest, rel in entries),
                                       encoding="utf-8")
    return entries


def verify_run(root: Path, repetitions: int | Mapping[str, int] | None = None) -> dict[str, Any]:
    if not root.is_dir():
        raise ResultLayoutError(f"run directory does not exist: {root}")
    if sorted(p.name for p in root.iterdir()) != sorted(RUN_ITEMS):
        raise ResultLayoutError("run root contains missing or unexpected entries")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    stored_plan = manifest.get("fault_repetitions")
    if stored_plan is None:
        n = manifest.get("repetitions_per_fault")
        validate_repetitions(n)
        stored_plan = {fault: n for fault in FAULTS}
    plan = repetition_plan(stored_plan)
    if repetitions is not None and repetition_plan(repetitions) != plan:
        raise ResultLayoutError("manifest repetition plan mismatch")
    active_faults = [fault for fault in FAULTS if plan[fault] > 0]
    active_counts = {plan[fault] for fault in active_faults}
    uniform = next(iter(active_counts)) if len(active_counts) == 1 else None
    if manifest.get("status") != "SUCCESS":
        raise ResultLayoutError("only a fully finalized SUCCESS run may be verified for publication")
    physical_count = sum(plan.values())
    if (manifest.get("physical_fault_instances") != physical_count
            or manifest.get("rca_inputs") != physical_count * 2
            or manifest.get("faults") != active_faults
            or manifest.get("treatments") != list(TREATMENTS)):
        raise ResultLayoutError("manifest counts or experiment grid do not match fault repetitions")
    ids = expected_case_ids(plan)
    gt = json.loads((root / "ground-truth.json").read_text(encoding="utf-8"))
    if sum(len(values) for values in gt.values()) != physical_count or set(gt) != set(active_faults):
        raise ResultLayoutError("Ground Truth must contain exactly one entry per physical instance")
    for fault in active_faults:
        if set(gt[fault]) != {f"R{r:02d}" for r in range(1, plan[fault] + 1)}:
            raise ResultLayoutError(f"Ground Truth repetition grid is incomplete for {fault}")
    config = manifest.get("configuration", {})
    rca_enabled = config.get("rca", {}).get("enabled", True)
    evaluation_enabled = config.get("evaluation", {}).get("enabled", True)
    for relative, expected in (("telemetry", set(ids)), ("llm/inputs", set(ids)),
                               ("llm/responses", set(ids) if rca_enabled else set())):
        directory = root / relative
        entries = list(directory.iterdir())
        if any(not path.is_file() or path.suffix != ".json" for path in entries):
            raise ResultLayoutError(f"{relative} must contain only flat JSON case files")
        actual = {path.stem for path in entries}
        if actual != expected:
            raise ResultLayoutError(f"{relative} identity mismatch: missing={sorted(expected-actual)}, extra={sorted(actual-expected)}")
    evaluation_dir = root / "evaluation"
    if [p.name for p in evaluation_dir.iterdir()] != ["results.json"]:
        raise ResultLayoutError("evaluation directory must contain only results.json")
    evaluation = json.loads((evaluation_dir / "results.json").read_text(encoding="utf-8"))
    if evaluation_enabled and not rca_enabled:
        raise ResultLayoutError("evaluation cannot be enabled when RCA is disabled")
    if not evaluation_enabled and evaluation.get("status") != "NOT_RUN":
        raise ResultLayoutError("disabled evaluation must be represented as NOT_RUN")
    hashes_path = root / "hashes.sha256"
    expected_hashes: dict[str, str] = {}
    for line in hashes_path.read_text(encoding="utf-8").splitlines():
        try:
            digest, rel = line.split("  ", 1)
        except ValueError as exc:
            raise ResultLayoutError("malformed run hash entry") from exc
        if (len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)
                or rel in expected_hashes or Path(rel).is_absolute() or ".." in Path(rel).parts):
            raise ResultLayoutError("invalid or duplicate run hash path")
        expected_hashes[rel] = digest
    actual_paths = {p.relative_to(root).as_posix() for p in root.rglob("*")
                    if p.is_file() and p != hashes_path}
    if actual_paths != set(expected_hashes):
        raise ResultLayoutError("run outer hash inventory is incomplete")
    for rel, digest in expected_hashes.items():
        if hashlib.sha256((root / rel).read_bytes()).hexdigest() != digest:
            raise ResultLayoutError(f"run hash mismatch: {rel}")
    raw_root = root / "raw"
    if sorted(p.name for p in raw_root.iterdir()) != ["A", "B"]:
        raise ResultLayoutError("raw must contain only A and B")
    for side in TREATMENTS:
        if sorted(p.name for p in (raw_root / side).iterdir()) != ["logs", "metrics", "traces"]:
            raise ResultLayoutError(f"raw/{side} modality layout is invalid")
    if sorted(p.name for p in (root / "llm").iterdir()) != ["inputs", "responses"]:
        raise ResultLayoutError("llm must contain only inputs and responses")
    return {"run_id": manifest.get("run_id"), "repetitions_per_fault": uniform,
            "fault_repetitions": plan,
            "physical_fault_instances": physical_count, "treatments": 2,
            "rca_inputs": physical_count * 2, "hash_entries": len(expected_hashes)}


def next_run_id(results_root: Path) -> str:
    children = results_root.iterdir() if results_root.is_dir() else ()
    used = {int(match.group(1)) for child in children
            if (match := re.fullmatch(r"run-([0-9]{3,})", child.name))}
    number = 1
    while number in used:
        number += 1
    return f"run-{number:03d}"


def publish_atomic(staged_run: Path, results_root: Path, run_id: str) -> Path:
    """Publish a complete verified run without overwriting an existing identity."""
    verify_run(staged_run)
    destination = results_root / run_id
    results_root.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise ResultLayoutError(f"run identity already exists: {run_id}")
    os.replace(staged_run, destination)
    verify_run(destination)
    return destination
