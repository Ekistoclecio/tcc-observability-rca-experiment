"""Offline deterministic evaluator for the canonical experiment run layout."""

from __future__ import annotations

import argparse
from decimal import Decimal
import json
import math
from pathlib import Path
import statistics
from typing import Any, Mapping, Sequence

from scripts.experiment.result_layout import (
    FAULTS, TREATMENTS, expected_case_ids, parse_case_id, repetition_plan,
)

EXPECTED_LOCATION_KINDS = {"service", "service_edge", "operation", "datastore"}
EXPECTED_FAULT_TYPES = {"cpu_saturation", "network_latency", "service_unavailable",
                        "application_exception", "datastore_unavailable"}
QUALITY_METRICS = ("accuracy_at_1", "accuracy_at_3", "reciprocal_rank",
                   "location_accuracy", "fault_type_accuracy")
OPERATIONAL_METRICS = ("input_tokens", "cached_input_tokens", "output_tokens",
                       "reasoning_tokens", "total_tokens", "estimated_cost_usd", "latency_ms")


class EvaluationError(ValueError):
    """Raised when frozen run responses or independent Ground Truth are invalid."""


def ground_truth_location(root_location: Any) -> dict[str, Any]:
    """Parse canonical Ground Truth kind/value into the exact typed schema."""
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
            raise EvaluationError("service_edge must be canonical 'source -> destination'")
        result["service"], result["peer_service"] = parts
    elif kind == "operation":
        parts = value.split("/")
        if len(parts) != 2 or not all(parts):
            raise EvaluationError("operation must be canonical 'service/operation'")
        result["service"], result["operation"] = parts
    else:
        result["datastore"] = value
    return result


def exact_location_match(predicted: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    return all(predicted.get(key) == expected.get(key)
               for key in ("kind", "service", "peer_service", "operation", "datastore"))


def evaluate_case(prediction: Mapping[str, Any], ground_truth: Mapping[str, Any],
                  usage: Mapping[str, Any], latency_ms: Any) -> dict[str, Any]:
    hypotheses = prediction.get("hypotheses")
    if not isinstance(hypotheses, list) or len(hypotheses) != 3:
        raise EvaluationError("prediction must contain exactly three hypotheses")
    expected_location, expected_fault = ground_truth.get("location"), ground_truth.get("fault_type")
    if not isinstance(expected_location, Mapping) or expected_location.get("kind") not in EXPECTED_LOCATION_KINDS:
        raise EvaluationError("Ground Truth location is invalid")
    if expected_fault not in EXPECTED_FAULT_TYPES:
        raise EvaluationError("Ground Truth fault type is invalid")
    ranked = []
    for rank, hypothesis in enumerate(hypotheses, 1):
        if not isinstance(hypothesis, Mapping) or hypothesis.get("rank") != rank:
            raise EvaluationError("prediction ranks must be [1, 2, 3]")
        location_ok = exact_location_match(hypothesis.get("location", {}), expected_location)
        type_ok = hypothesis.get("fault_type") == expected_fault
        ranked.append({"rank": rank, "location_correct": location_ok,
                       "fault_type_correct": type_ok,
                       "diagnosis_correct": location_ok and type_ok})
    correct = next((row["rank"] for row in ranked if row["diagnosis_correct"]), None)
    operational = {}
    for field in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens", "total_tokens"):
        value = usage.get(field)
        if value is not None and (type(value) is not int or value < 0):
            raise EvaluationError(f"invalid usage value: {field}")
        operational[field] = value
    cost = usage.get("estimated_cost_usd")
    if cost is not None:
        try:
            cost = Decimal(str(cost))
        except Exception as exc:
            raise EvaluationError("invalid estimated cost") from exc
        if not cost.is_finite() or cost < 0:
            raise EvaluationError("invalid estimated cost")
        cost = float(cost)
    if latency_ms is not None and (isinstance(latency_ms, bool)
            or not isinstance(latency_ms, (int, float)) or not math.isfinite(float(latency_ms))
            or latency_ms < 0):
        raise EvaluationError("invalid latency_ms")
    first = ranked[0]
    return {
        "rank_1_correct": first["diagnosis_correct"],
        "rank_of_correct_hypothesis": correct,
        "accuracy_at_1": int(first["diagnosis_correct"]),
        "accuracy_at_3": int(correct is not None),
        "reciprocal_rank": 0.0 if correct is None else round(1 / correct, 12),
        "location_accuracy": int(first["location_correct"]),
        "fault_type_accuracy": int(first["fault_type_correct"]),
        "ranked_match_details": ranked,
        **operational,
        "estimated_cost_usd": cost,
        "latency_ms": None if latency_ms is None else float(latency_ms),
    }


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationError(f"invalid or missing JSON artifact: {path}") from exc


def _mean(rows: Sequence[Mapping[str, Any]], metric: str) -> float | None:
    values = [row.get(metric) for row in rows if row.get(metric) is not None]
    return round(statistics.fmean(values), 12) if values else None


def _aggregate(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not rows:
        raise EvaluationError("cannot aggregate an empty treatment")
    result: dict[str, Any] = {"cases": len(rows)}
    for metric in QUALITY_METRICS:
        result[metric] = _mean(rows, metric)
        result[f"{metric}_valid_cases"] = sum(row.get(metric) is not None for row in rows)
    for metric in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens", "total_tokens"):
        values = [row.get(metric) for row in rows]
        total = sum(values) if all(value is not None for value in values) else None
        result["total_tokens" if metric == "total_tokens" else f"total_{metric}"] = total
        result[f"mean_{metric}_per_case"] = _mean(rows, metric)
    totals = [row.get("total_tokens") for row in rows if row.get("total_tokens") is not None]
    result["median_total_tokens_per_case"] = statistics.median(totals) if totals else None
    costs = [Decimal(str(row["estimated_cost_usd"])) for row in rows
             if row.get("estimated_cost_usd") is not None]
    result["total_estimated_cost_usd"] = float(sum(costs, Decimal(0))) if len(costs) == len(rows) else None
    result["mean_estimated_cost_usd_per_case"] = (
        float(sum(costs, Decimal(0)) / Decimal(len(rows))) if len(costs) == len(rows) else None)
    latencies = [float(row["latency_ms"]) for row in rows if row.get("latency_ms") is not None]
    result["mean_latency_ms"] = round(statistics.fmean(latencies), 12) if latencies else None
    result["median_latency_ms"] = statistics.median(latencies) if latencies else None
    result["min_latency_ms"] = min(latencies) if latencies else None
    result["max_latency_ms"] = max(latencies) if latencies else None
    return result


def _delta(a: Any, b: Any) -> Any:
    if a is None or b is None:
        return None
    if isinstance(a, (int, float)) and not isinstance(a, bool) \
            and isinstance(b, (int, float)) and not isinstance(b, bool):
        return round(float(Decimal(str(b)) - Decimal(str(a))), 12)
    return None


def _paired(a: Mapping[str, Any], b: Mapping[str, Any]) -> dict[str, Any]:
    aggregate_fields = {
        "input_tokens": "total_input_tokens",
        "cached_input_tokens": "total_cached_input_tokens",
        "output_tokens": "total_output_tokens",
        "reasoning_tokens": "total_reasoning_tokens",
        "total_tokens": "total_tokens",
        "estimated_cost_usd": "total_estimated_cost_usd",
        "latency_ms": "mean_latency_ms",
    }
    result = {}
    for metric in (*QUALITY_METRICS, *OPERATIONAL_METRICS):
        field = aggregate_fields.get(metric, metric)
        result[metric] = {"A": a.get(field), "B": b.get(field),
                          "delta_B_minus_A": _delta(a.get(field), b.get(field))}
    return result


def evaluate_run_layout(run_root: Path) -> dict[str, Any]:
    """Evaluate all response files against independent run Ground Truth."""
    run_root = run_root.resolve(strict=True)
    manifest = _read_json(run_root / "manifest.json")
    repetitions = manifest.get("fault_repetitions")
    if repetitions is None:
        uniform = manifest.get("repetitions_per_fault")
        if type(uniform) is not int or uniform < 1:
            raise EvaluationError("manifest must contain a valid fault repetition plan")
        repetitions = {fault: uniform for fault in FAULTS}
    try:
        plan = repetition_plan(repetitions)
    except ValueError as exc:
        raise EvaluationError(f"manifest fault repetition plan is invalid: {exc}") from exc
    active_faults = [fault for fault in FAULTS if plan[fault] > 0]
    expected = set(expected_case_ids(plan))
    gt_tree = _read_json(run_root / "ground-truth.json")
    response_dir = run_root / "llm" / "responses"
    response_files = {path.stem: path for path in response_dir.glob("*.json")}
    if set(response_files) != expected:
        raise EvaluationError("response identities do not match the run fault/repetition grid")
    cases: dict[str, dict[str, dict[str, Any]]] = {}
    for identity in sorted(expected):
        _, fault, repetition, treatment = parse_case_id(identity)
        rep_key = f"R{repetition:02d}"
        gt = gt_tree.get(fault, {}).get(rep_key)
        if not isinstance(gt, dict) or gt.get("status") != "RECOVERED" or gt.get("execution_eligible") is not True:
            raise EvaluationError(f"Ground Truth missing or ineligible for {identity}")
        response = _read_json(response_files[identity])
        status = response.get("status")
        usage = response.get("usage") or {}
        if status == "COMPLETED" and response.get("validation", {}).get("semantic") == "PASS":
            metrics = evaluate_case(response["prediction"],
                                    {"location": ground_truth_location(gt["root_location"]),
                                     "fault_type": gt["fault_type"]}, usage,
                                    response.get("latency_ms"))
        else:
            metrics = {metric: None for metric in QUALITY_METRICS}
            metrics.update({"rank_1_correct": None, "rank_of_correct_hypothesis": None,
                            "ranked_match_details": [], **{key: usage.get(key) for key in OPERATIONAL_METRICS if key != "latency_ms"},
                            "latency_ms": response.get("latency_ms")})
        record = {"response_status": status, "response_file": f"llm/responses/{identity}.json",
                  "input_file": f"llm/inputs/{identity}.json", **metrics}
        cases.setdefault(fault, {}).setdefault(rep_key, {})[treatment] = record
    fault_results: dict[str, Any] = {}
    for fault in active_faults:
        # Build rows directly from the nested case map to avoid relying on order.
        by_treatment = {t: [rep[t] for rep in cases[fault].values()] for t in TREATMENTS}
        a, b = _aggregate(by_treatment["A"]), _aggregate(by_treatment["B"])
        fault_results[fault] = {"repetitions": plan[fault], "treatment_A": a,
                                "treatment_B": b, "delta_B_minus_A": _paired(a, b)}
    overall = {}
    all_rows = {t: [cases[fault][rep][t] for fault in active_faults for rep in sorted(cases[fault])]
                for t in TREATMENTS}
    a, b = _aggregate(all_rows["A"]), _aggregate(all_rows["B"])
    overall = {"treatment_A": a, "treatment_B": b, "delta_B_minus_A": _paired(a, b)}
    return {"evaluation_version": "deterministic-rca-evaluation-v2",
            "run_id": manifest.get("run_id"), "purpose": manifest.get("purpose"),
            "repetitions_per_fault": (next(iter({plan[f] for f in active_faults}))
                                       if len({plan[f] for f in active_faults}) == 1 else None),
            "fault_repetitions": plan, "physical_fault_instances": sum(plan.values()),
            "cases": cases, "faults": fault_results, "overall": overall,
            "delta_convention": "B - A", "llm_judge": "NONE",
            "statistical_generalization_claim": False}


def write_evaluation(run_root: Path) -> dict[str, Any]:
    result = evaluate_run_layout(run_root)
    target = run_root / "evaluation" / "results.json"
    if target.exists():
        raise EvaluationError("evaluation/results.json already exists; refusing overwrite")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True,
                                 indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = write_evaluation(args.run_dir)
    except Exception as exc:
        print(json.dumps({"status": "BLOCKED", "error_type": type(exc).__name__}, sort_keys=True))
        return 2
    print(json.dumps({"status": "PASS", "run_id": result["run_id"],
                      "cases": result["physical_fault_instances"] * 2, "api_calls": 0}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
