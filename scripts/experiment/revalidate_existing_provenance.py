from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from scripts.experiment.dataset.core import read_json
from scripts.experiment.integration_validation import IntegrationValidation, validate_gt_document, write_json
from scripts.experiment.rca.projection_provenance import PROJECTION_VERSION


def revalidate(run_id: str, validation_id: str, output_dir: Path) -> dict:
    dataset_root = REPO / "artifacts/datasets" / run_id
    request_root = REPO / "artifacts/rca-inputs" / run_id
    gt_path = REPO / "artifacts/experiments" / run_id / "ground-truth.json"
    for path in (dataset_root / "scenario-a/dataset.json", dataset_root / "scenario-b/dataset.json",
                 request_root / "scenario-a/request.json", request_root / "scenario-b/request.json",
                 request_root / "scenario-a/manifest.json", request_root / "scenario-b/manifest.json", gt_path):
        if not path.is_file():
            raise FileNotFoundError(f"required existing run artifact missing: {path}")
    datasets = {scenario: read_json(dataset_root / f"scenario-{scenario.lower()}/dataset.json")
                for scenario in ("A", "B")}
    requests = {scenario: read_json(request_root / f"scenario-{scenario.lower()}/request.json")
                for scenario in ("A", "B")}
    manifests = {scenario: read_json(request_root / f"scenario-{scenario.lower()}/manifest.json")
                 for scenario in ("A", "B")}
    gt = read_json(gt_path)
    gt_audit = validate_gt_document(gt, "F4", run_id)
    if not gt_audit["passed"]:
        raise ValueError(f"existing F4 Ground Truth validation failed: {gt_audit['checks']}")

    validator = IntegrationValidation.__new__(IntegrationValidation)
    validator.validation_id = validation_id
    audit = validator.audit_requests(run_id, output_dir, datasets, dataset_root, request_root,
                                     requests["A"], requests["B"], manifests)
    checks = dict(audit["checks"])
    checks["ground_truth_valid"] = gt_audit["passed"]
    checks["projection_version_recorded"] = all(
        manifest.get("model_visible_projection_version") in (None, PROJECTION_VERSION)
        for manifest in manifests.values()
    )
    audit["checks"] = checks
    audit["passed"] = all(checks.values())
    output_dir.mkdir(parents=True, exist_ok=False)
    write_json(output_dir / "provenance-revalidation.json", {
        "run_id": run_id,
        "projection_version": PROJECTION_VERSION,
        "ground_truth_path": gt_path.relative_to(REPO).as_posix(),
        "ground_truth_sha256": hashlib.sha256(gt_path.read_bytes()).hexdigest(),
        "ground_truth_validation": gt_audit,
        "request_audit": audit,
    })
    lines = [
        "# F4 Provenance Revalidation",
        "",
        f"Run: `{run_id}` (existing artifacts; no fault was re-executed)",
        f"Projection contract: `{PROJECTION_VERSION}`",
        f"Ground Truth: **{'PASS' if gt_audit['passed'] else 'FAIL'}**",
        "",
        "| Scenario | Dataset → Request | Allowed removals | Unexpected removals | Unexpected additions | Unexpected mutations | Evidence IDs | Counts |",
        "|---|---|---|---:|---:|---:|---|---|",
    ]
    for scenario in ("A", "B"):
        item = audit["telemetry_integrity"][scenario]
        lines.append(
            f"| {scenario} | {'PASS' if item['passed'] else 'FAIL'} | `{json.dumps(item['allowed_removals'], sort_keys=True)}` "
            f"| {len(item['unexpected_removals'])} | {len(item['unexpected_additions'])} "
            f"| {len(item['unexpected_mutations'])} | {'PASS' if item['evidence_ids']['match'] else 'FAIL'} "
            f"| {'PASS' if item['item_counts_match'] else 'FAIL'} |"
        )
    lines += ["", f"Overall provenance: **{'PASS' if audit['passed'] else 'FAIL'}**", "",
              f"Machine-readable evidence: `{(output_dir / 'provenance-revalidation.json').relative_to(REPO)}`", ""]
    (output_dir / "provenance-revalidation.md").write_text("\n".join(lines), encoding="utf-8")
    return audit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Revalidate Dataset-to-RCA-input provenance without rerunning a fault")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--validation-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        output = args.output_dir if args.output_dir.is_absolute() else REPO / args.output_dir
        audit = revalidate(args.run_id, args.validation_id, output.resolve())
        print(json.dumps({"passed": audit["passed"], "scenarios": {
            scenario: audit["telemetry_integrity"][scenario] for scenario in ("A", "B")
        }}, indent=2, sort_keys=True))
        return 0 if audit["passed"] else 1
    except Exception as exc:
        print(f"provenance revalidation: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
