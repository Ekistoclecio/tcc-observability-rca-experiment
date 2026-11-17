from __future__ import annotations

import json
import hashlib
from pathlib import Path
import tempfile
import unittest

from scripts.experiment.evaluation.evaluator import (
    EvaluationError,
    _write_outputs,
    _delta,
    aggregate_treatment,
    build_evaluation,
    evaluate_case,
    exact_location_match,
    ground_truth_location,
    verify_hash_manifest,
)


def location(kind="service", service="service-alpha", peer=None, operation=None, datastore=None):
    return {"kind": kind, "service": service, "peer_service": peer,
            "operation": operation, "datastore": datastore}


def hypothesis(rank, loc=None, fault="cpu_saturation"):
    return {"rank": rank, "location": loc or location(), "fault_type": fault,
            "confidence": 1.0 - (rank - 1) * 0.2, "evidence_ids": [],
            "explanation": "Synthetic test evidence supports this hypothesis."}


def usage(cost="0.004", offset=0):
    return {"input_tokens": 100 + offset, "cached_input_tokens": 10,
            "output_tokens": 20 + offset, "reasoning_tokens": 7,
            "total_tokens": 120 + 2 * offset,
            "estimated_cost_usd": cost, "cost_status": "ESTIMATED"}


def gt(loc=None, fault="cpu_saturation"):
    return {"location": loc or location(), "fault_type": fault}


class ExactScoringTests(unittest.TestCase):
    def test_rank_one_correct(self):
        result = evaluate_case({"hypotheses": [hypothesis(1), hypothesis(2, location("service", "service-beta")), hypothesis(3, location("service", "service-gamma"), "network_latency")]}, gt(), usage(), 100.0)
        self.assertEqual(result["accuracy_at_1"], 1)
        self.assertEqual(result["accuracy_at_3"], 1)
        self.assertEqual(result["reciprocal_rank"], 1.0)
        self.assertEqual(result["location_accuracy"], 1)
        self.assertEqual(result["fault_type_accuracy"], 1)

    def test_correct_rank_two(self):
        result = evaluate_case({"hypotheses": [hypothesis(1, location("service", "service-beta")), hypothesis(2), hypothesis(3, location("service", "service-gamma"), "network_latency")]}, gt(), usage(), 100.0)
        self.assertEqual((result["accuracy_at_1"], result["accuracy_at_3"], result["reciprocal_rank"]), (0, 1, 0.5))
        self.assertEqual((result["location_accuracy"], result["fault_type_accuracy"]), (0, 1))

    def test_correct_rank_three(self):
        result = evaluate_case({"hypotheses": [hypothesis(1, location("service", "service-beta")), hypothesis(2, location("service", "service-gamma"), "network_latency"), hypothesis(3)]}, gt(), usage(), 100.0)
        self.assertEqual((result["accuracy_at_1"], result["accuracy_at_3"]), (0, 1))
        self.assertAlmostEqual(result["reciprocal_rank"], 1 / 3)

    def test_no_correct_hypothesis(self):
        result = evaluate_case({"hypotheses": [hypothesis(1, location("service", "service-beta")), hypothesis(2, location("service", "service-gamma"), "network_latency"), hypothesis(3, location("service", "service-delta"), "datastore_unavailable")]}, gt(), usage(), 100.0)
        self.assertEqual((result["accuracy_at_1"], result["accuracy_at_3"], result["reciprocal_rank"]), (0, 0, 0.0))

    def test_correct_location_wrong_fault_is_partial_only(self):
        result = evaluate_case({"hypotheses": [hypothesis(1, fault="network_latency"), hypothesis(2, location("service", "service-beta")), hypothesis(3, location("service", "service-gamma"), "datastore_unavailable")]}, gt(), usage(), 100.0)
        self.assertEqual((result["location_accuracy"], result["fault_type_accuracy"], result["accuracy_at_1"]), (1, 0, 0))

    def test_wrong_location_correct_fault_is_partial_only(self):
        result = evaluate_case({"hypotheses": [hypothesis(1, location("service", "service-beta")), hypothesis(2, location("service", "service-gamma"), "network_latency"), hypothesis(3, location("service", "service-delta"), "datastore_unavailable")]}, gt(), usage(), 100.0)
        self.assertEqual((result["location_accuracy"], result["fault_type_accuracy"], result["accuracy_at_1"]), (0, 1, 0))

    def test_operation_is_not_parent_service(self):
        expected = ground_truth_location({"kind": "operation", "value": "service-alpha/OperationX"})
        predicted = location("service", "service-alpha")
        self.assertFalse(exact_location_match(predicted, expected))
        result = evaluate_case({"hypotheses": [hypothesis(1, predicted), hypothesis(2, location("service", "service-beta")), hypothesis(3, location("service", "service-gamma"), "network_latency")]}, {"location": expected, "fault_type": "cpu_saturation"}, usage(), 10.0)
        self.assertEqual(result["location_accuracy"], 0)

    def test_service_edge_direction_is_exact(self):
        expected = ground_truth_location({"kind": "service_edge", "value": "service-alpha -> service-beta"})
        reversed_edge = location("service_edge", "service-beta", "service-alpha")
        self.assertFalse(exact_location_match(reversed_edge, expected))
        self.assertTrue(exact_location_match(expected, expected))

    def test_other_location_kinds_parse_to_exact_typed_location(self):
        self.assertEqual(ground_truth_location({"kind": "service", "value": "service-alpha"}), location())
        self.assertEqual(ground_truth_location({"kind": "datastore", "value": "datastore-x"}), location("datastore", None, datastore="datastore-x"))

    def test_unknown_ground_truth_encoding_fails_closed(self):
        with self.assertRaises(EvaluationError):
            ground_truth_location({"kind": "service_edge", "value": "service-alpha-service-beta"})

    def test_nested_hash_manifest_is_an_inventory_member(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / "bundle" / "hashes.sha256"
            nested.parent.mkdir()
            nested.write_text("synthetic nested manifest\n")
            payload = root / "bundle" / "artifact.json"
            payload.write_text("{}\n")
            entries = sorted((nested, payload))
            outer = "".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(root).as_posix()}\n" for p in entries)
            (root / "hashes.sha256").write_text(outer)
            verified = verify_hash_manifest(root)
            self.assertEqual(set(verified), {"bundle/hashes.sha256", "bundle/artifact.json"})
            nested.write_text("modified nested manifest\n")
            with self.assertRaises(EvaluationError):
                verify_hash_manifest(root)

    def test_non_three_hypotheses_and_rank_order_fail(self):
        with self.assertRaises(EvaluationError):
            evaluate_case({"hypotheses": [hypothesis(1), hypothesis(2)]}, gt(), usage(), 10)
        with self.assertRaises(EvaluationError):
            evaluate_case({"hypotheses": [hypothesis(2), hypothesis(1), hypothesis(3)]}, gt(), usage(), 10)


class OperationalMetricsTests(unittest.TestCase):
    def test_usage_and_frozen_cost_are_propagated_without_retokenization(self):
        record = evaluate_case({"hypotheses": [hypothesis(1), hypothesis(2, location("service", "service-beta")), hypothesis(3, location("service", "service-gamma"), "network_latency")]}, gt(), usage("0.0054321", 5), 1234.5)
        self.assertEqual(record["input_tokens"], 105)
        self.assertEqual(record["cached_input_tokens"], 10)
        self.assertEqual(record["output_tokens"], 25)
        self.assertEqual(record["reasoning_tokens"], 7)
        self.assertEqual(record["total_tokens"], 130)
        self.assertEqual(record["estimated_cost_usd"], 0.0054321)
        self.assertEqual(record["latency_ms"], 1234.5)

    def test_cost_must_come_from_frozen_usage(self):
        incomplete = usage()
        incomplete.pop("estimated_cost_usd")
        with self.assertRaises(EvaluationError):
            evaluate_case({"hypotheses": [hypothesis(1), hypothesis(2, location("service", "service-beta")), hypothesis(3, location("service", "service-gamma"), "network_latency")]}, gt(), incomplete, 1)

    def test_delta_uses_decimal_difference_without_float_artifacts(self):
        self.assertEqual(_delta(28951.4, 38616.2), 9664.8)
        self.assertEqual(_delta(13587.461, 10516.598), -3070.863)

    def test_aggregates_five_cases_exactly(self):
        rows = []
        for i in range(5):
            rows.append({"accuracy_at_1": i % 2, "accuracy_at_3": 1,
                         "reciprocal_rank": [1.0, 0.5, 1 / 3, 0.0, 1.0][i],
                         "location_accuracy": int(i < 3), "fault_type_accuracy": int(i >= 2),
                         "input_tokens": 100 + i, "cached_input_tokens": i,
                         "output_tokens": 20 + i, "reasoning_tokens": 7 + i,
                         "total_tokens": 120 + 2 * i,
                         "estimated_cost_usd": DecimalCost("0.001"),
                         "latency_ms": 1000 + i * 100})
        aggregate = aggregate_treatment(rows)
        self.assertEqual(aggregate["cases"], 5)
        self.assertEqual(aggregate["accuracy_at_1"], 0.4)
        self.assertEqual(aggregate["accuracy_at_3"], 1.0)
        self.assertEqual(aggregate["total_input_tokens"], 510)
        self.assertEqual(aggregate["total_cached_input_tokens"], 10)
        self.assertEqual(aggregate["total_output_tokens"], 110)
        self.assertEqual(aggregate["total_reasoning_tokens"], 45)
        self.assertEqual(aggregate["total_tokens"], 620)
        self.assertEqual(aggregate["median_total_tokens_per_case"], 124)
        self.assertEqual(aggregate["total_estimated_cost_usd"], 0.005)
        self.assertEqual(aggregate["mean_latency_ms"], 1200)
        self.assertEqual(aggregate["median_latency_ms"], 1200)

    def test_deterministic_output_bytes_and_hash_inventory(self):
        rows = []
        for treatment in ("A", "B"):
            for i in range(5):
                hypotheses = [hypothesis(1), hypothesis(2, location("service", "service-beta")), hypothesis(3, location("service", "service-gamma"), "network_latency")]
                if treatment == "B" and i == 0:
                    hypotheses = [hypothesis(1, location("service", "service-beta")), hypothesis(2), hypothesis(3, location("service", "service-gamma"), "network_latency")]
                rows.append({"case_id": f"case-{treatment}-{i}",
                             "physical_fault_id": f"fault-{i}", "fault_id": f"fault-{i}", "fault_label": f"F{i+1}",
                             "fault_dir": f"fault-{i}", "treatment": treatment,
                             "input_path": f"input-{treatment}-{i}", "input_sha256": "a" * 64,
                             "ground_truth_path": f"gt-{i}", "ground_truth_sha256": "b" * 64,
                             "ground_truth": gt(), "prediction_path": f"pred-{treatment}-{i}",
                             "prediction_sha256": "c" * 64, "request_fingerprint": "d" * 64,
                             "latency_clean": True,
                             "prediction": {"hypotheses": hypotheses},
                             "usage": usage("0.006" if treatment == "B" else "0.004",
                                             10 if treatment == "B" else 0),
                             "latency_ms": 1100 + i if treatment == "B" else 1000 + i})
        source = {"cases": rows, "protocol": {"protocol_version": "test-v1"},
                  "prompt_sha256": "1" * 64, "schema_sha256": "2" * 64,
                  "plan_sha256": "3" * 64, "input_hashes": {f"i{i}": str(i) for i in range(10)},
                  "pilot_inventory_count": 50, "results_inventory_count": 100,
                  "audit_inventory_count": 20}
        evaluation = build_evaluation(source, "evaluator-test-commit")
        self.assertEqual(evaluation["paired"]["delta_convention"], "B - A")
        self.assertEqual(evaluation["summary"]["treatment_A"]["cases"], 5)
        with tempfile.TemporaryDirectory() as tmp_a, tempfile.TemporaryDirectory() as tmp_b:
            first, second = Path(tmp_a) / "evaluation", Path(tmp_b) / "evaluation"
            _write_outputs(evaluation, first)
            _write_outputs(evaluation, second)
            verify_hash_manifest(first)
            verify_hash_manifest(second)
            files_a = {p.relative_to(first).as_posix(): p.read_bytes() for p in first.rglob("*") if p.is_file()}
            files_b = {p.relative_to(second).as_posix(): p.read_bytes() for p in second.rglob("*") if p.is_file()}
            self.assertEqual(files_a, files_b)
            readme = (first / "README.md").read_text()
            self.assertNotRegex(readme, r"(?m)[ \t]+$")
            paired = json.loads((first / "paired-comparison.json").read_text())
            self.assertEqual(paired["delta_convention"], "B - A")
        self.assertEqual(paired["faults"]["fault-0"]["metrics"]["accuracy_at_1"]["delta_B_minus_A"], -1.0)
        self.assertEqual(paired["faults"]["fault-0"]["metrics"]["input_tokens"]["delta_B_minus_A"], 10.0)
        self.assertEqual(evaluation["summary"]["delta_B_minus_A"]["accuracy_at_1"], -0.2)
        self.assertEqual(evaluation["summary"]["delta_B_minus_A"]["total_tokens"], 100)
        self.assertEqual(evaluation["summary"]["delta_B_minus_A"]["total_estimated_cost_usd"], 0.01)


def DecimalCost(value: str) -> float:
    # Test fixture helper: deliberately supplies the frozen numeric artifact value.
    return float(value)


if __name__ == "__main__":
    unittest.main()
