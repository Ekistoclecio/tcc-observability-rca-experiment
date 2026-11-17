"""Deterministic evaluator for the frozen experiment-v2 RCA pilot.

This module has no OpenAI client or network dependency. Ground Truth is read only
in the evaluation path, after pilot predictions and evaluator rules are frozen.
"""

from __future__ import annotations

import argparse
from decimal import Decimal
import hashlib
import json
import math
import re
import statistics
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[3]
EXPERIMENT = ROOT / "docs/experiment"
RESULTS = EXPERIMENT / "results"
AUDIT = EXPERIMENT / "teste"
LLM = EXPERIMENT / "llm"
PILOT = LLM / "pilot-v1"
PROMPT = LLM / "rca-prompt-v1.txt"
SCHEMA = LLM / "rca-schema-v1.json"
PROTOCOL = LLM / "protocol-v1.json"
INPUT_MANIFEST = LLM / "rca-inputs.sha256"
OUTPUT = EXPERIMENT / "evaluation/pilot-v1"

RUNNER_COMMIT = "17e37b7fd07720f1e10cad52865d7974d102eb79"
PREDICTIONS_COMMIT = "552c114029c66cbd2aa9309757876f69907b2cba"
EXPECTED_LOCATION_KINDS = {"service", "service_edge", "operation", "datastore"}
EXPECTED_FAULT_TYPES = {
    "cpu_saturation", "network_latency", "service_unavailable",
    "application_exception", "datastore_unavailable",
}
QUALITY_METRICS = (
    "accuracy_at_1", "accuracy_at_3", "reciprocal_rank",
    "location_accuracy", "fault_type_accuracy",
)
OPERATIONAL_METRICS = (
    "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens",
    "total_tokens", "estimated_cost_usd", "latency_ms",
)
PAIRED_METRICS = QUALITY_METRICS + OPERATIONAL_METRICS


class EvaluationError(ValueError):
    """Raised when frozen pilot, Ground Truth, or evaluator input is invalid."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode("utf-8")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationError(f"cannot read JSON artifact: {path.relative_to(ROOT)}") from exc


def verify_hash_manifest(root: Path) -> dict[str, str]:
    """Verify an outer inventory; exclude only this root's own hashes.sha256."""
    manifest = root / "hashes.sha256"
    if not root.is_dir() or not manifest.is_file():
        raise EvaluationError(f"missing artifact inventory: {root}")
    expected: dict[str, str] = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        try:
            digest, relative = line.split("  ", 1)
        except ValueError as exc:
            raise EvaluationError(f"malformed hash entry in {manifest}") from exc
        posix = PurePosixPath(relative)
        if (len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)
                or posix.is_absolute() or ".." in posix.parts or not relative
                or relative in expected):
            raise EvaluationError(f"invalid or duplicate inventory path: {relative}")
        expected[relative] = digest
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*") if path.is_file() and path != manifest
    }
    if actual != set(expected):
        missing, unlisted = sorted(set(expected) - actual), sorted(actual - set(expected))
        raise EvaluationError(
            f"inventory coverage mismatch at {root}: missing={missing[:3]}, unlisted={unlisted[:3]}"
        )
    for relative, digest in expected.items():
        if _sha((root / relative).read_bytes()) != digest:
            raise EvaluationError(f"artifact SHA-256 mismatch: {relative}")
    return expected


def _read_input_hashes() -> dict[str, str]:
    protocol = _read_json(PROTOCOL)
    raw = INPUT_MANIFEST.read_bytes()
    if _sha(raw) != protocol.get("input_hash_manifest_sha256"):
        raise EvaluationError("frozen RCA Input manifest hash mismatch")
    entries: dict[str, str] = {}
    for line in raw.decode("utf-8").splitlines():
        digest, relative = line.split("  ", 1)
        posix = PurePosixPath(relative)
        if (len(digest) != 64 or posix.is_absolute() or ".." in posix.parts
                or relative in entries):
            raise EvaluationError(f"invalid frozen input manifest entry: {relative}")
        entries[relative] = digest
    if len(entries) != 10:
        raise EvaluationError(f"expected ten frozen RCA Inputs, found {len(entries)}")
    for relative, digest in entries.items():
        path = (ROOT / relative).resolve(strict=True)
        if not path.is_relative_to(RESULTS.resolve()) or _sha(path.read_bytes()) != digest:
            raise EvaluationError(f"frozen RCA Input mismatch: {relative}")
    return entries


def _verify_git_provenance(run_manifest: Mapping[str, Any]) -> None:
    runner_commit = run_manifest.get("runner_commit")
    if runner_commit != RUNNER_COMMIT:
        raise EvaluationError("pilot runner commit does not match frozen provenance")
    try:
        prediction_commit = subprocess.check_output(
            ["git", "log", "-1", "--format=%H", "--",
             "docs/experiment/llm/pilot-v1/summary.json"],
            cwd=ROOT, text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise EvaluationError("cannot verify pilot prediction commit") from exc
    if prediction_commit != PREDICTIONS_COMMIT:
        raise EvaluationError("pilot prediction commit does not match frozen provenance")


def verify_frozen_artifacts() -> dict[str, Any]:
    """Verify all relevant frozen inventories and protocol/input provenance."""
    results_hashes = verify_hash_manifest(RESULTS)
    audit_hashes = verify_hash_manifest(AUDIT)
    pilot_hashes = verify_hash_manifest(PILOT)

    protocol = _read_json(PROTOCOL)
    prompt_hash = _sha(PROMPT.read_bytes())
    schema = _read_json(SCHEMA)
    schema_hash = _sha(_canonical_json(schema))
    if prompt_hash != protocol.get("prompt_sha256"):
        raise EvaluationError("frozen prompt hash mismatch")
    if schema_hash != protocol.get("schema_sha256"):
        raise EvaluationError("frozen schema hash mismatch")

    input_hashes = _read_input_hashes()
    plan_path = PILOT / "execution-plan.json"
    plan_bytes = plan_path.read_bytes()
    plan_hash = _sha(plan_bytes)
    plan = json.loads(plan_bytes)
    run_manifest = _read_json(PILOT / "run-manifest.json")
    if plan_hash != run_manifest.get("execution_plan_sha256"):
        raise EvaluationError("pilot execution-plan hash mismatch")
    if plan.get("logical_call_count") != 10 or len(plan.get("cases", [])) != 10:
        raise EvaluationError("pilot execution plan does not contain ten cases")
    if run_manifest.get("prompt_sha256") != prompt_hash:
        raise EvaluationError("pilot prompt provenance mismatch")
    if run_manifest.get("schema_sha256") != schema_hash:
        raise EvaluationError("pilot schema provenance mismatch")
    if run_manifest.get("protocol_manifest_sha256") != _sha(PROTOCOL.read_bytes()):
        raise EvaluationError("pilot protocol-manifest hash mismatch")
    _verify_git_provenance(run_manifest)

    plan_by_id = {case["opaque_case_id"]: case for case in plan["cases"]}
    if len(plan_by_id) != 10:
        raise EvaluationError("duplicate opaque IDs in pilot execution plan")
    ordered_cases = sorted(plan["cases"], key=lambda row: row.get("ordinal", 0))
    if [row.get("ordinal") for row in ordered_cases] != list(range(1, 11)):
        raise EvaluationError("pilot execution plan ordinals are not exactly 1 through 10")
    for planned in ordered_cases:
        digest = planned.get("input_sha256", "")
        expected_order = hashlib.sha256(
            ("llm-rca-pilot-v1-order:" + digest).encode("ascii")
        ).hexdigest()
        expected_case_id = "case-" + hashlib.sha256(
            ("llm-rca-pilot-v1-case:" + digest).encode("ascii")
        ).hexdigest()[:12]
        if planned.get("order_key") != expected_order or planned.get("opaque_case_id") != expected_case_id:
            raise EvaluationError("pilot execution plan ordering or opaque ID is invalid")
    private_map = run_manifest.get("case_map_private")
    if not isinstance(private_map, list) or len(private_map) != 10:
        raise EvaluationError("pilot private case mapping is incomplete")
    if run_manifest.get("request_parallelism") != 1:
        raise EvaluationError("pilot did not use the frozen sequential request policy")
    map_by_id = {row.get("opaque_case_id"): row for row in private_map}
    if set(map_by_id) != set(plan_by_id):
        raise EvaluationError("pilot private mapping differs from execution plan")

    input_to_case: dict[str, dict[str, str]] = {}
    for opaque_id, row in map_by_id.items():
        relative = row.get("input_path")
        digest = row.get("input_sha256")
        planned = plan_by_id[opaque_id]
        if (relative not in input_hashes or input_hashes[relative] != digest
                or planned.get("input_sha256") != digest):
            raise EvaluationError(f"input mapping/hash mismatch for {opaque_id}")
        if digest in input_to_case:
            raise EvaluationError("duplicate frozen input hash in private case mapping")
        if not relative.endswith(("rca-input-A.json", "rca-input-B.json")):
            raise EvaluationError(f"unexpected input treatment path: {relative}")
        treatment = "A" if relative.endswith("rca-input-A.json") else "B"
        source = (ROOT / relative).resolve(strict=True)
        if not source.is_relative_to(RESULTS.resolve()):
            raise EvaluationError("private input mapping escapes frozen results")
        case_dir = PILOT / "cases" / opaque_id
        request_meta = _read_json(case_dir / "request-metadata.json")
        response_meta = _read_json(case_dir / "response-metadata.json")
        validation = _read_json(case_dir / "validation.json")
        prediction_path = case_dir / "prediction.json"
        prediction = _read_json(prediction_path)
        if (request_meta.get("status") != "COMPLETED"
                or request_meta.get("input_sha256") != digest
                or request_meta.get("opaque_case_id") != opaque_id
                or request_meta.get("ordinal") != planned.get("ordinal")
                or request_meta.get("runner_protocol_version") != protocol.get("protocol_version")
                or request_meta.get("attempt_count") != 1
                or request_meta.get("latency_clean") is not True
                or validation.get("status") != "PASS"
                or not response_meta.get("response_id")
                or response_meta.get("api_status") != "completed"
                or response_meta.get("requested_model") != protocol.get("model")
                or response_meta.get("returned_model") != protocol.get("model")
                or response_meta.get("service_tier_requested") != protocol.get("service_tier")
                or response_meta.get("service_tier_returned") != protocol.get("service_tier")):
            raise EvaluationError(f"pilot case is not a clean completed response: {opaque_id}")
        if _sha(source.read_bytes()) != digest:
            raise EvaluationError(f"input bytes changed for {opaque_id}")

        # Reuse the frozen offline protocol validator. It has no network or SDK use.
        from scripts.experiment.llm.protocol import extract_evidence_ids
        from scripts.experiment.llm.response_validator import validate_rca_response
        rca_input = json.loads(source.read_bytes())
        extract_evidence_ids(rca_input)
        validate_rca_response(prediction, rca_input)
        from scripts.experiment.llm.execution import request_identity_fields
        expected_fingerprint = request_identity_fields(digest)["request_fingerprint"]
        if request_meta.get("request_fingerprint") != expected_fingerprint:
            raise EvaluationError(f"request fingerprint mismatch for {opaque_id}")

        gt_path = source.parent / "ground-truth.json"
        gt = _read_json(gt_path)
        if gt.get("status") != "RECOVERED" or gt.get("execution_eligible") is not True:
            raise EvaluationError(f"Ground Truth is not an eligible recovered execution: {gt_path}")
        location = ground_truth_location(gt.get("root_location"))
        fault_id = gt.get("fault_id")
        if not isinstance(fault_id, str) or not re.match(r"^F[1-5](?:-|$)", fault_id):
            raise EvaluationError(f"unexpected physical fault identifier in Ground Truth: {gt_path}")
        fault_label = fault_id.split("-", 1)[0]
        audit_fault = _read_json(AUDIT / "faults" / f"{fault_label}.json")
        if (audit_fault.get("fault") != fault_label
                or audit_fault.get("fault_type") != gt.get("fault_type")
                or audit_fault.get("location") != gt.get("root_location")):
            raise EvaluationError(f"Ground Truth does not match independent audit mapping: {fault_label}")

        usage = response_meta.get("usage")
        request_usage = request_meta.get("usage")
        if not isinstance(usage, dict) or usage != request_usage:
            raise EvaluationError(f"runner usage metadata mismatch for {opaque_id}")
        input_to_case[digest] = {
            "case_id": opaque_id,
            "input_path": relative,
            "input_sha256": digest,
            "treatment": treatment,
            "fault_id": fault_id,
            "fault_label": fault_label,
            "fault_dir": source.parent.name,
            "ground_truth_path": gt_path.relative_to(ROOT).as_posix(),
            "ground_truth_sha256": _sha(gt_path.read_bytes()),
            "ground_truth": {
                "fault_type": gt["fault_type"],
                "location": location,
            },
            "prediction_path": prediction_path.relative_to(ROOT).as_posix(),
            "prediction_sha256": _sha(prediction_path.read_bytes()),
            "prediction": prediction,
            "usage": usage,
            "latency_ms": request_meta["latency_ms"],
            "latency_clean": request_meta["latency_clean"],
            "request_fingerprint": request_meta.get("request_fingerprint"),
        }
    if len(input_to_case) != 10 or set(input_hashes) != {
            record["input_path"] for record in private_map}:
        raise EvaluationError("pilot inputs are not a one-to-one match with frozen input manifest")

    by_fault: dict[str, set[str]] = {}
    for row in input_to_case.values():
        by_fault.setdefault(row["fault_id"], set()).add(row["treatment"])
    if len(by_fault) != 5 or any(treatments != {"A", "B"} for treatments in by_fault.values()):
        raise EvaluationError("expected five physical faults with one A/B pair each")

    return {
        "protocol": protocol,
        "prompt_sha256": prompt_hash,
        "schema_sha256": schema_hash,
        "input_hashes": input_hashes,
        "plan_sha256": plan_hash,
        "plan": plan,
        "run_manifest": run_manifest,
        "results_inventory_count": len(results_hashes),
        "audit_inventory_count": len(audit_hashes),
        "pilot_inventory_count": len(pilot_hashes),
        "cases": sorted(input_to_case.values(), key=lambda row: (row["fault_id"], row["treatment"])),
    }


def ground_truth_location(root_location: Any) -> dict[str, Any]:
    """Parse the canonical Ground Truth kind/value representation exactly."""
    if not isinstance(root_location, dict) or set(root_location) != {"kind", "value"}:
        raise EvaluationError("Ground Truth root_location must contain kind and value")
    kind, value = root_location["kind"], root_location["value"]
    if kind not in EXPECTED_LOCATION_KINDS or not isinstance(value, str) or not value:
        raise EvaluationError("Ground Truth root_location has invalid kind or value")
    result = {"kind": kind, "service": None, "peer_service": None,
              "operation": None, "datastore": None}
    if kind == "service":
        result["service"] = value
    elif kind == "service_edge":
        parts = value.split(" -> ")
        if len(parts) != 2 or not all(parts):
            raise EvaluationError("service_edge Ground Truth must use canonical 'source -> destination'")
        result["service"], result["peer_service"] = parts
    elif kind == "operation":
        parts = value.split("/")
        if len(parts) != 2 or not all(parts):
            raise EvaluationError("operation Ground Truth must use canonical 'service/operation'")
        result["service"], result["operation"] = parts
    else:
        result["datastore"] = value
    return result


def exact_location_match(predicted: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    """Strict kind-and-components equality; no aliases or fuzzy matching."""
    keys = ("kind", "service", "peer_service", "operation", "datastore")
    return all(predicted.get(key) == expected.get(key) for key in keys)


def _validate_operational(usage: Mapping[str, Any], latency_ms: Any) -> dict[str, Any]:
    fields = ("input_tokens", "cached_input_tokens", "output_tokens",
              "reasoning_tokens", "total_tokens")
    metrics: dict[str, Any] = {}
    for field in fields:
        value = usage.get(field)
        if type(value) is not int or value < 0:
            raise EvaluationError(f"pilot usage field is missing or invalid: {field}")
        metrics[field] = value
    cost = usage.get("estimated_cost_usd")
    if cost is None or usage.get("cost_status") != "ESTIMATED":
        raise EvaluationError("pilot case has no frozen estimated cost")
    try:
        cost_decimal = Decimal(str(cost))
    except Exception as exc:
        raise EvaluationError("pilot estimated cost is invalid") from exc
    if not cost_decimal.is_finite() or cost_decimal < 0:
        raise EvaluationError("pilot estimated cost is invalid")
    if isinstance(latency_ms, bool) or not isinstance(latency_ms, (int, float)):
        raise EvaluationError("pilot latency_ms is missing or invalid")
    if not math.isfinite(float(latency_ms)) or latency_ms < 0:
        raise EvaluationError("pilot latency_ms is invalid")
    metrics["estimated_cost_usd"] = float(cost_decimal)
    metrics["latency_ms"] = float(latency_ms)
    return metrics


def evaluate_case(case: Mapping[str, Any], ground_truth: Mapping[str, Any],
                  usage: Mapping[str, Any], latency_ms: float) -> dict[str, Any]:
    """Score one immutable prediction against one independent Ground Truth."""
    hypotheses = case.get("hypotheses")
    if not isinstance(hypotheses, list) or len(hypotheses) != 3:
        raise EvaluationError("prediction must contain exactly three frozen hypotheses")
    expected_location = ground_truth.get("location")
    expected_fault = ground_truth.get("fault_type")
    if not isinstance(expected_location, Mapping) or expected_location.get("kind") not in EXPECTED_LOCATION_KINDS:
        raise EvaluationError("evaluation Ground Truth location is invalid")
    if not isinstance(expected_fault, str) or expected_fault not in EXPECTED_FAULT_TYPES:
        raise EvaluationError("evaluation Ground Truth fault_type is invalid")

    ranked: list[dict[str, Any]] = []
    for rank, hypothesis in enumerate(hypotheses, 1):
        if not isinstance(hypothesis, Mapping) or hypothesis.get("rank") != rank:
            raise EvaluationError("frozen prediction ranks must be exactly [1, 2, 3]")
        loc_correct = exact_location_match(hypothesis.get("location", {}), expected_location)
        type_correct = hypothesis.get("fault_type") == expected_fault
        ranked.append({"rank": rank, "location_correct": loc_correct,
                       "fault_type_correct": type_correct,
                       "diagnosis_correct": loc_correct and type_correct})
    correct_ranks = [item["rank"] for item in ranked if item["diagnosis_correct"]]
    correct_rank = correct_ranks[0] if correct_ranks else None
    rr = 0.0 if correct_rank is None else round(1.0 / correct_rank, 12)
    rank1 = ranked[0]
    operational = _validate_operational(usage, latency_ms)
    return {
        "rank_1_correct": rank1["diagnosis_correct"],
        "rank_of_correct_hypothesis": correct_rank,
        "accuracy_at_1": int(rank1["diagnosis_correct"]),
        "accuracy_at_3": int(correct_rank is not None),
        "reciprocal_rank": rr,
        "location_accuracy": int(rank1["location_correct"]),
        "fault_type_accuracy": int(rank1["fault_type_correct"]),
        "ranked_match_details": ranked,
        **operational,
    }


def _round_metric(value: float) -> float:
    return round(value, 12)


def aggregate_treatment(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if len(rows) != 5:
        raise EvaluationError(f"each treatment must have five paired cases, found {len(rows)}")
    aggregate: dict[str, Any] = {"cases": len(rows)}
    for metric in QUALITY_METRICS:
        aggregate[metric] = _round_metric(statistics.fmean(float(row[metric]) for row in rows))
    for field in ("input_tokens", "cached_input_tokens", "output_tokens",
                  "reasoning_tokens", "total_tokens"):
        values = [int(row[field]) for row in rows]
        total_key = "total_tokens" if field == "total_tokens" else f"total_{field}"
        aggregate[total_key] = sum(values)
        aggregate[f"mean_{field}_per_case"] = _round_metric(statistics.fmean(values))
    aggregate["median_total_tokens_per_case"] = statistics.median(
        int(row["total_tokens"]) for row in rows
    )
    costs = [Decimal(str(row["estimated_cost_usd"])) for row in rows]
    total_cost = sum(costs, Decimal(0))
    aggregate["total_estimated_cost_usd"] = float(total_cost)
    aggregate["mean_estimated_cost_usd_per_case"] = float(total_cost / Decimal(len(rows)))
    latencies = [float(row["latency_ms"]) for row in rows]
    aggregate["mean_latency_ms"] = _round_metric(statistics.fmean(latencies))
    aggregate["median_latency_ms"] = _round_metric(statistics.median(latencies))
    aggregate["min_latency_ms"] = min(latencies)
    aggregate["max_latency_ms"] = max(latencies)
    return aggregate


def _delta(a: Any, b: Any) -> Any:
    if isinstance(a, bool) or isinstance(b, bool):
        return None
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return _round_metric(float(b) - float(a))
    return None


def _paired_metrics(a: Mapping[str, Any], b: Mapping[str, Any]) -> dict[str, Any]:
    return {metric: {"A": a[metric], "B": b[metric], "delta_B_minus_A": _delta(a[metric], b[metric])}
            for metric in PAIRED_METRICS}


def _case_record(source: Mapping[str, Any]) -> dict[str, Any]:
    metrics = evaluate_case(source["prediction"], source["ground_truth"],
                            source["usage"], source["latency_ms"])
    return {
        "case_id": source["case_id"],
        "physical_fault_id": source["fault_id"],
        "fault_label": source["fault_label"],
        "fault_instance": source["fault_dir"],
        "treatment": source["treatment"],
        "input_path": source["input_path"],
        "input_sha256": source["input_sha256"],
        "ground_truth_path": source["ground_truth_path"],
        "ground_truth_sha256": source["ground_truth_sha256"],
        "ground_truth": source["ground_truth"],
        "prediction_path": source["prediction_path"],
        "prediction_sha256": source["prediction_sha256"],
        "request_fingerprint": source["request_fingerprint"],
        "latency_clean": source["latency_clean"],
        **metrics,
    }


def _metric_table(rows: Sequence[Mapping[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    result = {}
    for fault_id in sorted({row["physical_fault_id"] for row in rows}):
        pair = {row["treatment"]: row for row in rows if row["physical_fault_id"] == fault_id}
        if set(pair) != {"A", "B"}:
            raise EvaluationError(f"incomplete A/B pair for physical fault {fault_id}")
        a, b = pair["A"][field], pair["B"][field]
        result[fault_id] = {"A": a, "B": b, "delta_B_minus_A": _delta(a, b)}
    return result


def _markdown_report(evaluation: Mapping[str, Any]) -> str:
    summary = evaluation["summary"]
    a, b, delta = summary["treatment_A"], summary["treatment_B"], summary["delta_B_minus_A"]
    lines = [
        "# Avaliação piloto determinística RCA — v1", "",
        "Esta é uma avaliação piloto de cinco faults físicos pareados, uma execução por fault. "
        "Não sustenta conclusões estatísticas generalizáveis. Nenhum LLM-as-a-Judge, teste de "
        "significância ou avaliação qualitativa foi aplicado. Tokens, custo e latência são "
        "operacionais; `delta = B - A` e sinal positivo não significa automaticamente melhor.", "",
        f"Evaluator commit: `{evaluation['manifest']['evaluator_commit']}`  ",
        f"Predictions commit: `{PREDICTIONS_COMMIT}`  ",
        f"Ground Truth mapping and artifact integrity: `{evaluation['manifest']['integrity']}`", "",
        "## Métricas diagnósticas", "",
        "| Treatment | Cases | Acc@1 | Acc@3 | MRR | Location Acc. | Fault Type Acc. |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for label, row in (("A", a), ("B", b), ("Δ B−A", delta)):
        lines.append("| {} | {} | {} | {} | {} | {} | {} |".format(
            label, row.get("cases", "—"), row["accuracy_at_1"], row["accuracy_at_3"],
            row["reciprocal_rank"], row["location_accuracy"], row["fault_type_accuracy"]))
    lines += ["", "## Consumo por treatment", "",
              "| Treatment | Input | Cached input | Output | Reasoning | Total | Cost USD | Mean latency ms | Median latency ms |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for label, row in (("A", a), ("B", b), ("Δ B−A", delta)):
        lines.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
            label, row.get("total_input_tokens", row.get("total_input_tokens_delta", row.get("total_input_tokens", "—"))),
            row.get("total_cached_input_tokens", "—"), row.get("total_output_tokens", "—"),
            row.get("total_reasoning_tokens", "—"), row.get("total_tokens", "—"),
            row.get("total_estimated_cost_usd", "—"), row.get("mean_latency_ms", "—"),
            row.get("median_latency_ms", "—")))
    lines += ["", "## Resultados pareados por fault", "",
              "Os campos de qualidade usam métricas do caso; `Δ` é sempre B−A.", "",
              "| Fault | Treatment | Rank correto | Acc@1 | Acc@3 | RR | Location | Type | Tokens | Cost USD | Latency ms |",
              "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for row in evaluation["cases"]:
        lines.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
            row["fault_label"], row["treatment"], row["rank_of_correct_hypothesis"] or "—",
            row["accuracy_at_1"], row["accuracy_at_3"], row["reciprocal_rank"],
            row["location_accuracy"], row["fault_type_accuracy"], row["total_tokens"],
            row["estimated_cost_usd"], row["latency_ms"]))
    lines += ["", "## Proveniência e limites", "",
              "A unidade pareada é a mesma execução física, identificada pelo Ground Truth independente. "
              "Location exige igualdade exata de kind e componentes, incluindo direção da aresta. "
              "Accuracy@k requer location e fault type corretos simultaneamente; Location Accuracy e "
              "Fault Type Accuracy usam somente rank 1. Não foram feitas normalizações fuzzy.", "",
              "Nenhuma API foi chamada nesta etapa. Ground Truth foi usado somente pelo evaluator. "
              "Não foram calculados Evidence Precision, Hallucination Rate, p-values, intervalos de "
              "confiança ou métricas por LLM judge.", ""]
    return "\n".join(lines)


def build_evaluation(source: Mapping[str, Any], evaluator_commit: str) -> dict[str, Any]:
    case_rows = [_case_record(row) for row in source["cases"]]
    if len(case_rows) != 10:
        raise EvaluationError("expected ten evaluated predictions")
    treatment_rows = {
        treatment: [row for row in case_rows if row["treatment"] == treatment]
        for treatment in ("A", "B")
    }
    aggregates = {treatment: aggregate_treatment(rows)
                  for treatment, rows in treatment_rows.items()}
    delta = {key: _delta(aggregates["A"].get(key), aggregates["B"].get(key))
             for key in aggregates["A"] if key != "cases"}
    paired = {
        "delta_convention": "B - A",
        "unit": "same physical fault execution",
        "faults": {
            fault_id: {"fault_label": next(row["fault_label"] for row in case_rows
                                           if row["physical_fault_id"] == fault_id),
                       "metrics": _paired_metrics(
                           next(row for row in case_rows if row["physical_fault_id"] == fault_id and row["treatment"] == "A"),
                           next(row for row in case_rows if row["physical_fault_id"] == fault_id and row["treatment"] == "B"))}
            for fault_id in sorted({row["physical_fault_id"] for row in case_rows})
        },
        "aggregate_delta_B_minus_A": delta,
    }
    protocol = source["protocol"]
    manifest = {
        "evaluation_version": "deterministic-rca-pilot-v1",
        "evaluator_commit": evaluator_commit,
        "runner_commit": RUNNER_COMMIT,
        "predictions_commit": PREDICTIONS_COMMIT,
        "protocol_version": protocol["protocol_version"],
        "prompt_sha256": source["prompt_sha256"],
        "schema_sha256": source["schema_sha256"],
        "execution_plan_sha256": source["plan_sha256"],
        "frozen_input_count": len(source["input_hashes"]),
        "pilot_inventory_entries": source["pilot_inventory_count"],
        "results_inventory_entries": source["results_inventory_count"],
        "audit_inventory_entries": source["audit_inventory_count"],
        "ground_truth_files": len({row["ground_truth_path"] for row in source["cases"]}),
        "integrity": "PASS",
        "llm_api_calls": 0,
        "llm_judge": "NONE",
        "ground_truth_used_by_evaluator": True,
        "pilot_only": True,
        "statistical_generalization_claim": False,
        "significance_testing": False,
    }
    summary = {
        "evaluation_version": manifest["evaluation_version"],
        "pilot_only": True,
        "physical_fault_instances": 5,
        "cases_per_treatment": 5,
        "evaluated_predictions": len(case_rows),
        "treatment_A": aggregates["A"],
        "treatment_B": aggregates["B"],
        "delta_B_minus_A": delta,
        "delta_convention": "B - A",
        "statistical_generalization_claim": False,
        "significance_testing": False,
        "llm_judge": "NONE",
        "llm_api_calls_during_evaluation": 0,
    }
    paired["aggregate_delta_B_minus_A"] = delta
    files: dict[str, Any] = {
        **{f"cases/{row['case_id']}.json": row for row in case_rows},
        "aggregate-A.json": aggregates["A"],
        "aggregate-B.json": aggregates["B"],
        "paired-comparison.json": paired,
        "summary.json": summary,
        "evaluation-manifest.json": manifest,
    }
    return {"cases": case_rows, "aggregate_A": aggregates["A"],
            "aggregate_B": aggregates["B"], "paired": paired,
            "summary": summary, "manifest": manifest, "files": files}


def _write_outputs(evaluation: Mapping[str, Any], output: Path = OUTPUT) -> None:
    if output.exists():
        raise EvaluationError(f"evaluation output already exists; refusing to overwrite: {output}")
    output.mkdir(parents=True)
    for relative, value in evaluation["files"].items():
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_json_bytes(value))
    (output / "README.md").write_text(_markdown_report(evaluation), encoding="utf-8")
    entries = []
    for path in sorted(p for p in output.rglob("*") if p.is_file() and p.name != "hashes.sha256"):
        entries.append(f"{_sha(path.read_bytes())}  {path.relative_to(output).as_posix()}")
    (output / "hashes.sha256").write_text("\n".join(entries) + "\n", encoding="utf-8")
    verify_hash_manifest(output)


def evaluate_frozen_pilot(output: Path = OUTPUT) -> dict[str, Any]:
    source = verify_frozen_artifacts()
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain=v1"], cwd=ROOT, text=True).strip()
    if dirty:
        raise EvaluationError("working tree must be clean before evaluating frozen predictions")
    result = build_evaluation(source, head)
    _write_outputs(result, output)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    try:
        result = evaluate_frozen_pilot(args.output)
    except Exception as exc:
        print(json.dumps({"status": "BLOCKED", "error_type": type(exc).__name__}, sort_keys=True))
        return 2
    print(json.dumps({"status": "PASS", "evaluator_commit": result["manifest"]["evaluator_commit"],
                      "cases": len(result["cases"]), "api_calls": 0,
                      "output": args.output.relative_to(ROOT).as_posix()}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
