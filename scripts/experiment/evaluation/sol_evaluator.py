"""Independent offline evaluator for the isolated GPT-6.1 Sol E2E pilot."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Sequence

from scripts.experiment.evaluation import evaluator as metrics
from scripts.experiment.llm import runner
from scripts.experiment.llm.protocol import ROOT, canonical_json_bytes, sha256
from scripts.experiment.llm.response_validator import validate_rca_response


def load_sol_source(protocol_path: Path, run_root: Path, physical_root: Path) -> dict[str, Any]:
    """Verify frozen Sol inputs, predictions, and independent Ground Truth mapping."""
    protocol = runner.configure_runner(protocol_path)
    protocol_path = protocol_path.resolve(strict=True)
    run_root = run_root.resolve(strict=True)
    physical_root = physical_root.resolve(strict=True)
    llm_root = protocol_path.parent
    prompt_path = ROOT / protocol["prompt_file"]
    schema_path = ROOT / protocol["schema_file"]
    if sha256(prompt_path.read_bytes()) != protocol["prompt_sha256"]:
        raise metrics.EvaluationError("Sol prompt hash mismatch")
    schema_obj = json.loads(schema_path.read_text(encoding="utf-8"))
    if sha256(canonical_json_bytes(schema_obj)) != protocol["schema_sha256"]:
        raise metrics.EvaluationError("Sol schema hash mismatch")

    input_hashes = runner.verify_frozen_inputs()
    if len(input_hashes) != 10:
        raise metrics.EvaluationError("Sol frozen input set must contain ten RCA Inputs")
    physical_results = physical_root / "results"
    physical_audit = physical_root / "teste"
    results_inventory = metrics.verify_hash_manifest(physical_results)
    audit_inventory = metrics.verify_hash_manifest(physical_audit)
    run_inventory = metrics.verify_hash_manifest(run_root)

    plan_path = llm_root / Path(protocol["execution_plan_file"]).name
    plan_bytes = plan_path.read_bytes()
    plan = json.loads(plan_bytes)
    plan_hash = sha256(plan_bytes)
    run_manifest = json.loads((run_root / "run-manifest.json").read_text(encoding="utf-8"))
    if (run_manifest.get("execution_plan_sha256") != plan_hash
            or run_manifest.get("protocol_manifest_sha256") != sha256(protocol_path.read_bytes())
            or run_manifest.get("prompt_sha256") != protocol["prompt_sha256"]
            or run_manifest.get("schema_sha256") != protocol["schema_sha256"]
            or run_manifest.get("protocol_version") != protocol["protocol_version"]
            or run_manifest.get("request_parallelism") != 1):
        raise metrics.EvaluationError("Sol run provenance does not match frozen protocol/plan")
    if plan.get("logical_call_count") != 10 or len(plan.get("cases", [])) != 10:
        raise metrics.EvaluationError("Sol execution plan must contain ten cases")

    mapping = run_manifest.get("case_map_private")
    plan_by_id = {row["opaque_case_id"]: row for row in plan["cases"]}
    map_by_id = {row["opaque_case_id"]: row for row in mapping or []}
    if len(map_by_id) != 10 or set(map_by_id) != set(plan_by_id):
        raise metrics.EvaluationError("Sol private input mapping is incomplete")

    cases = []
    for case_id, private in map_by_id.items():
        planned = plan_by_id[case_id]
        relative = private["input_path"]
        digest = private["input_sha256"]
        if input_hashes.get(relative) != digest or planned["input_sha256"] != digest:
            raise metrics.EvaluationError(f"Sol input hash mapping mismatch for {case_id}")
        source_path = (ROOT / relative).resolve(strict=True)
        if not source_path.is_relative_to(physical_results.resolve()):
            raise metrics.EvaluationError("Sol input escapes isolated physical results")
        source_bytes = source_path.read_bytes()
        if hashlib.sha256(source_bytes).hexdigest() != digest:
            raise metrics.EvaluationError(f"Sol RCA Input changed for {case_id}")
        rca_input = json.loads(source_bytes)
        case_dir = run_root / "cases" / case_id
        request_meta = json.loads((case_dir / "request-metadata.json").read_text(encoding="utf-8"))
        response_meta = json.loads((case_dir / "response-metadata.json").read_text(encoding="utf-8"))
        validation = json.loads((case_dir / "validation.json").read_text(encoding="utf-8"))
        prediction_path = case_dir / "prediction.json"
        prediction = json.loads(prediction_path.read_text(encoding="utf-8"))
        expected_fingerprint = runner.request_fingerprint(
            runner.build_openai_rca_request(rca_input), digest)
        if (request_meta.get("status") != "COMPLETED"
                or request_meta.get("opaque_case_id") != case_id
                or request_meta.get("input_sha256") != digest
                or request_meta.get("request_fingerprint") != expected_fingerprint
                or request_meta.get("attempt_count") != 1
                or request_meta.get("latency_clean") is not True
                or response_meta.get("requested_model") != "gpt-6.1-sol"
                or response_meta.get("returned_model") != "gpt-6.1-sol"
                or response_meta.get("api_status") != "completed"
                or validation.get("status") != "PASS"):
            raise metrics.EvaluationError(f"Sol case is not a valid clean completion: {case_id}")
        validate_rca_response(prediction, rca_input)

        if not relative.endswith(("rca-input-A.json", "rca-input-B.json")):
            raise metrics.EvaluationError(f"unexpected Sol input treatment path: {relative}")
        treatment = "A" if relative.endswith("rca-input-A.json") else "B"
        gt_path = source_path.parent / "ground-truth.json"
        gt = json.loads(gt_path.read_text(encoding="utf-8"))
        if gt.get("status") != "RECOVERED" or gt.get("execution_eligible") is not True:
            raise metrics.EvaluationError(f"ineligible Sol physical Ground Truth: {gt_path}")
        fault_id = gt.get("fault_id")
        label = fault_id.split("-", 1)[0] if isinstance(fault_id, str) else ""
        audit_fault_path = physical_audit / "faults" / f"{label}.json"
        audit_fault = json.loads(audit_fault_path.read_text(encoding="utf-8"))
        if (audit_fault.get("fault") != label or audit_fault.get("fault_type") != gt.get("fault_type")
                or audit_fault.get("location") != gt.get("root_location")):
            raise metrics.EvaluationError(f"Sol Ground Truth differs from physical audit mapping: {label}")
        cases.append({
            "case_id": case_id, "fault_id": fault_id, "fault_label": label,
            "fault_dir": source_path.parent.name, "treatment": treatment,
            "input_path": relative, "input_sha256": digest,
            "ground_truth_path": gt_path.relative_to(ROOT).as_posix(),
            "ground_truth_sha256": sha256(gt_path.read_bytes()),
            "ground_truth": {"fault_type": gt["fault_type"],
                             "location": metrics.ground_truth_location(gt["root_location"])},
            "prediction_path": prediction_path.relative_to(ROOT).as_posix(),
            "prediction_sha256": sha256(prediction_path.read_bytes()),
            "prediction": prediction, "usage": response_meta.get("usage"),
            "latency_ms": request_meta.get("latency_ms"),
            "latency_clean": request_meta.get("latency_clean"),
            "request_fingerprint": request_meta.get("request_fingerprint"),
        })
    if len({case["input_sha256"] for case in cases}) != 10:
        raise metrics.EvaluationError("Sol input set contains duplicate content")
    pairing = {}
    for case in cases:
        pairing.setdefault(case["fault_id"], set()).add(case["treatment"])
    if len(pairing) != 5 or any(pair != {"A", "B"} for pair in pairing.values()):
        raise metrics.EvaluationError("Sol physical inputs do not form five A/B pairs")

    summary_path = run_root / "summary.json"
    try:
        prediction_commit = subprocess.check_output(
            ["git", "log", "-1", "--format=%H", "--", summary_path.relative_to(ROOT).as_posix()],
            cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise metrics.EvaluationError("cannot verify committed Sol predictions") from exc
    if not prediction_commit:
        raise metrics.EvaluationError("Sol predictions have not been committed")
    return {
        "protocol": protocol, "prompt_sha256": protocol["prompt_sha256"],
        "schema_sha256": protocol["schema_sha256"], "input_hashes": input_hashes,
        "plan_sha256": plan_hash, "plan": plan, "run_manifest": run_manifest,
        "results_inventory_count": len(results_inventory),
        "audit_inventory_count": len(audit_inventory),
        "pilot_inventory_count": len(run_inventory), "cases": cases,
        "runner_commit": run_manifest["runner_commit"],
        "predictions_commit": prediction_commit,
        "evaluation_version": "deterministic-sol-e2e-v1",
    }


def evaluate_sol_pilot(protocol_path: Path, run_root: Path, physical_root: Path,
                       output: Path) -> dict[str, Any]:
    if output.exists():
        raise metrics.EvaluationError("Sol evaluation output already exists")
    source = load_sol_source(protocol_path, run_root, physical_root)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain=v1"], cwd=ROOT, text=True).strip()
    if dirty:
        raise metrics.EvaluationError("working tree must be clean before Sol evaluation")
    result = metrics.build_evaluation(source, head)
    result["manifest"]["evaluation_version"] = source["evaluation_version"]
    result["summary"]["evaluation_version"] = source["evaluation_version"]
    for relative, value in result["files"].items():
        if relative in ("evaluation-manifest.json", "summary.json"):
            result["files"][relative] = result["manifest"] if relative == "evaluation-manifest.json" else result["summary"]
    metrics._write_outputs(result, output)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--physical-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    output_path = args.output if args.output.is_absolute() else ROOT / args.output
    try:
        result = evaluate_sol_pilot(args.protocol, args.run_root, args.physical_root, output_path)
    except Exception as exc:
        print(json.dumps({"status": "BLOCKED", "error_type": type(exc).__name__}, sort_keys=True))
        return 2
    print(json.dumps({"status": "PASS", "cases": len(result["cases"]),
                      "evaluator_commit": result["manifest"]["evaluator_commit"],
                      "output": output_path.relative_to(ROOT).as_posix()}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
