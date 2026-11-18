from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from scripts.experiment.evaluation.evaluator import evaluate_run_layout
from scripts.experiment.result_layout import (
    FAULTS, TREATMENTS, ResultLayoutError, build_ground_truth, expected_case_ids, instance_ids,
    next_run_id, outer_hashes, parse_case_id, publish_atomic, validate_repetitions, verify_run,
    write_json,
)


def ground_truth(identity: str) -> dict:
    return {"schema_version": "1.0", "fault_id": identity,
            "fault_type": "cpu_saturation",
            "root_location": {"kind": "service", "value": "service-alpha"},
            "status": "RECOVERED", "execution_eligible": True,
            "requested_start": "2026-01-01T00:00:00Z",
            "confirmed_start": "2026-01-01T00:00:01Z",
            "confirmed_end": "2026-01-01T00:00:20Z"}


def prediction() -> dict:
    return {"hypotheses": [
        {"rank": 1, "location": {"kind": "service", "service": "service-alpha",
         "peer_service": None, "operation": None, "datastore": None},
         "fault_type": "cpu_saturation"},
        {"rank": 2, "location": {"kind": "service", "service": "service-beta",
         "peer_service": None, "operation": None, "datastore": None},
         "fault_type": "network_latency"},
        {"rank": 3, "location": {"kind": "datastore", "service": None,
         "peer_service": None, "operation": None, "datastore": "datastore-x"},
         "fault_type": "datastore_unavailable"},
    ]}


def make_run(root: Path, repetitions: int) -> None:
    (root / "raw").mkdir(parents=True)
    for side in "AB":
        for modality in ("metrics", "logs", "traces"):
            path = root / "raw" / side / modality / "chunk-0001.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n")
    instances = {}
    for identity in instance_ids(repetitions):
        fault, rep = identity.split("-R")
        instances[identity] = {"ground_truth": ground_truth(identity)}
        repetition = int(rep)
        for treatment in "AB":
            case = f"{identity}-{treatment}"
            for folder in ("telemetry", "llm/inputs", "llm/responses"):
                (root / folder).mkdir(parents=True, exist_ok=True)
            write_json(root / "telemetry" / f"{case}.json", {"synthetic": case})
            write_json(root / "llm" / "inputs" / f"{case}.json", {
                "model": "gpt-6-luna", "input": [{"role": "user", "content": "synthetic"}],
            })
            write_json(root / "llm" / "responses" / f"{case}.json", {
                "status": "COMPLETED", "prediction": prediction(),
                "validation": {"semantic": "PASS"}, "latency_ms": 1000 + repetition,
                "usage": {"input_tokens": 100, "cached_input_tokens": 10,
                          "output_tokens": 20, "reasoning_tokens": 5,
                          "total_tokens": 120, "estimated_cost_usd": "0.001"},
            })
    (root / "evaluation").mkdir()
    write_json(root / "evaluation" / "results.json", {})
    write_json(root / "ground-truth.json", build_ground_truth(instances))
    write_json(root / "manifest.json", {
        "run_id": "run-001", "status": "SUCCESS",
        "purpose": "result_layout_end_to_end_validation",
        "repetitions_per_fault": repetitions, "physical_fault_instances": 5 * repetitions,
        "rca_inputs": 10 * repetitions, "faults": list(FAULTS),
        "treatments": list(TREATMENTS),
    })
    outer_hashes(root)


class RepetitionLayoutTests(unittest.TestCase):
    def test_instance_and_case_names_scale_without_collisions(self):
        self.assertEqual(instance_ids(1), [f"F{i}-R01" for i in range(1, 6)])
        self.assertEqual(len(expected_case_ids(1)), 10)
        self.assertEqual(len(instance_ids(5)), 25)
        cases = expected_case_ids(5)
        self.assertEqual(len(cases), 50)
        self.assertEqual(cases[0], "F1-R01-A")
        self.assertEqual(cases[-1], "F5-R05-B")
        self.assertEqual(parse_case_id("F3-R04-B"), ("F3-R04-B", "F3", 4, "B"))
        with self.assertRaises(ResultLayoutError):
            validate_repetitions(0)

    def test_ground_truth_is_one_hierarchical_file_for_n1_and_n5(self):
        for n, expected in ((1, 5), (5, 25)):
            instances = {identity: {"ground_truth": ground_truth(identity)}
                         for identity in instance_ids(n)}
            grouped = build_ground_truth(instances)
            self.assertEqual(sum(map(len, grouped.values())), expected)
            self.assertEqual(set(grouped), {f"F{i}" for i in range(1, 6)})

    def test_complete_tree_and_evaluation_for_n1(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "run-001"
            make_run(root, 1)
            self.assertEqual(verify_run(root, 1)["physical_fault_instances"], 5)
            results = evaluate_run_layout(root)
            self.assertEqual(results["overall"]["treatment_A"]["cases"], 5)
            self.assertEqual(results["overall"]["treatment_B"]["accuracy_at_1"], 1.0)
            self.assertEqual(set(results), {"evaluation_version", "run_id", "purpose",
                "repetitions_per_fault", "physical_fault_instances", "cases", "faults",
                "overall", "delta_convention", "llm_judge", "statistical_generalization_claim"})

    def test_complete_tree_and_fault_pairs_scale_for_n5(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "run-001"
            make_run(root, 5)
            integrity = verify_run(root, 5)
            self.assertEqual(integrity["physical_fault_instances"], 25)
            self.assertEqual(integrity["rca_inputs"], 50)
            result = evaluate_run_layout(root)
            self.assertEqual(len(result["cases"]), 5)
            self.assertEqual(len(result["faults"]), 5)
            self.assertTrue(all(len(reps) == 5 for reps in result["cases"].values()))
            self.assertEqual(result["overall"]["treatment_A"]["cases"], 25)
            self.assertEqual(result["faults"]["F4"]["treatment_B"]["cases"], 5)

    def test_next_run_id_does_not_create_results_root_or_reuse_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "results"
            self.assertEqual(next_run_id(root), "run-001")
            self.assertFalse(root.exists())
            root.mkdir()
            (root / "run-001").mkdir()
            self.assertEqual(next_run_id(root), "run-002")

    def test_atomic_publication_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staged, published = root / ".staged", root / "results"
            make_run(staged, 1)
            published.mkdir()
            self.assertEqual(publish_atomic(staged, published, "run-001"), published / "run-001")
            self.assertTrue((published / "run-001" / "hashes.sha256").is_file())
            make_run(root / ".staged-again", 1)
            with self.assertRaises(ResultLayoutError):
                publish_atomic(root / ".staged-again", published, "run-001")


if __name__ == "__main__":
    unittest.main()
