from __future__ import annotations

import unittest

from scripts.experiment.evaluation.evaluator import (
    EvaluationError, evaluate_case, exact_location_match, ground_truth_location,
)


def location(kind="service", service="service-alpha", peer=None, operation=None, datastore=None):
    return {"kind": kind, "service": service, "peer_service": peer,
            "operation": operation, "datastore": datastore}


def hypothesis(rank, loc=None, fault="cpu_saturation"):
    return {"rank": rank, "location": loc or location(), "fault_type": fault}


def prediction(correct_rank=None, loc=None, fault="cpu_saturation"):
    rows = [hypothesis(1, location("service", "service-beta"), "network_latency"),
            hypothesis(2, location("service", "service-gamma"), "service_unavailable"),
            hypothesis(3, location("datastore", None, datastore="datastore-x"), "datastore_unavailable")]
    if correct_rank:
        rows[correct_rank - 1] = hypothesis(correct_rank, loc or location(), fault)
    return {"hypotheses": rows}


def usage():
    return {"input_tokens": 101, "cached_input_tokens": 10, "output_tokens": 25,
            "reasoning_tokens": 8, "total_tokens": 126, "estimated_cost_usd": "0.004"}


class DeterministicScoringTests(unittest.TestCase):
    def test_correct_ranks_one_two_three_and_missing(self):
        gt = {"location": location(), "fault_type": "cpu_saturation"}
        expected = ((1, 1, 1.0), (2, 0, 0.5), (3, 0, 1 / 3), (None, 0, 0.0))
        for rank, at1, rr in expected:
            result = evaluate_case(prediction(rank), gt, usage(), 123.5)
            self.assertEqual(result["accuracy_at_1"], at1)
            self.assertEqual(result["accuracy_at_3"], int(rank is not None))
            self.assertAlmostEqual(result["reciprocal_rank"], rr)
        result = evaluate_case(prediction(None), gt, usage(), 123.5)
        self.assertEqual(result["rank_of_correct_hypothesis"], None)

    def test_location_and_fault_type_are_separate_rank_one_metrics(self):
        gt = {"location": location(), "fault_type": "cpu_saturation"}
        correct_location_wrong_fault = evaluate_case(
            prediction(1, fault="network_latency"), gt, usage(), 10)
        self.assertEqual((correct_location_wrong_fault["location_accuracy"],
                          correct_location_wrong_fault["fault_type_accuracy"],
                          correct_location_wrong_fault["accuracy_at_1"]), (1, 0, 0))
        wrong_location_correct_fault = evaluate_case(
            prediction(1, loc=location("service", "service-zeta")), gt, usage(), 10)
        self.assertEqual((wrong_location_correct_fault["location_accuracy"],
                          wrong_location_correct_fault["fault_type_accuracy"],
                          wrong_location_correct_fault["accuracy_at_1"]), (0, 1, 0))

    def test_operation_is_not_parent_service_and_edge_direction_is_exact(self):
        operation = ground_truth_location({"kind": "operation", "value": "service-alpha/OperationX"})
        self.assertFalse(exact_location_match(location("service", "service-alpha"), operation))
        edge = ground_truth_location({"kind": "service_edge", "value": "service-alpha -> service-beta"})
        reversed_edge = location("service_edge", "service-beta", "service-alpha")
        self.assertFalse(exact_location_match(reversed_edge, edge))

    def test_cost_and_usage_are_propagated_without_recalculation(self):
        result = evaluate_case(prediction(1), {"location": location(), "fault_type": "cpu_saturation"},
                               usage(), 321.0)
        self.assertEqual(result["input_tokens"], 101)
        self.assertEqual(result["cached_input_tokens"], 10)
        self.assertEqual(result["output_tokens"], 25)
        self.assertEqual(result["reasoning_tokens"], 8)
        self.assertEqual(result["total_tokens"], 126)
        self.assertEqual(result["estimated_cost_usd"], 0.004)
        self.assertEqual(result["latency_ms"], 321.0)

    def test_unknown_ground_truth_encoding_fails_closed(self):
        with self.assertRaises(EvaluationError):
            ground_truth_location({"kind": "service_edge", "value": "service-alpha-service-beta"})


if __name__ == "__main__":
    unittest.main()
