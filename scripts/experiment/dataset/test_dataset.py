from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import patch

from .backends import BACKENDS, collect_loki
from .core import SUT_SERVICES, build_query_plan, epoch_microseconds, epoch_nanoseconds, window_from_ground_truth
from .normalize import normalize_scenario


def _ground_truth(component: str, fault_type: str) -> dict:
    return {
        "status": "valid", "validation": {"injection_confirmed": True, "recovery_confirmed": True},
        "fault": {"class": "F4", "type": fault_type, "location": {"component": component}, "parameters": {"secret": "must-not-leak"}},
        "timing": {"confirmed_start": "2026-10-28T05:19:54.631Z", "confirmed_end": "2026-10-28T05:20:03.996Z"},
    }


def _raw(scenario: str) -> dict:
    return {
        "metrics": {"response": {"data": {"resultType": "matrix", "result": [
            {"metric": {"__name__": "requests_total", "service_name": "checkoutservice", "method": "Checkout"},
             "values": [[1793164800, "2"]]}]}}, "metadata": {}},
        "logs": {"response": {"data": {"result": [{
            "stream": {"service_name": "checkoutservice", "severity": "INFO", "trace_id": "b-trace", "span_id": "b-span",
                       "x_tcc_trace_id": "a-trace", "x_tcc_span_id": "a-span"},
            "values": [["1793164800000000000", "checkout started"]]}]}}},
        "traces": {"search_results": [{"service": "checkoutservice", "response": {"data": [
            {"traceID": "trace-1", "processes": {"p1": {"serviceName": "checkoutservice"}},
             "spans": [{"spanID": "s1", "processID": "p1", "operationName": "Checkout", "startTime": 1793164800000000,
                        "duration": 1000, "references": [], "tags": []}]}]}}]},
    }


class DatasetBuilderTests(unittest.TestCase):
    def test_ground_truth_blind_query_plan(self):
        gt_a = _ground_truth("paymentservice", "application_exception")
        gt_b = _ground_truth("redis-cart", "datastore_unavailable")
        window_a = window_from_ground_truth(gt_a, 10, 10)
        window_b = window_from_ground_truth(gt_b, 10, 10)
        self.assertEqual(build_query_plan(window_a), build_query_plan(window_b))
        plan = build_query_plan(window_a)
        self.assertEqual(plan["A"][0]["params"], plan["B"][0]["params"])
        self.assertEqual(plan["A"][1]["params"], plan["B"][1]["params"])
        self.assertEqual(plan["A"][2]["search"], plan["B"][2]["search"])
        for query_a, query_b in zip(plan["A"], plan["B"]):
            self.assertEqual(query_a["path"], query_b["path"])
            self.assertEqual(query_a.get("params"), query_b.get("params"))
            self.assertEqual(query_a.get("search"), query_b.get("search"))
        encoded = str(plan)
        for service in SUT_SERVICES:
            self.assertIn(service, encoded)
        self.assertNotIn("application_exception", encoded)
        self.assertNotIn("datastore_unavailable", encoded)
        self.assertNotIn("fault", encoded)

    def test_window_epoch_conversion_is_exact(self):
        window = window_from_ground_truth(_ground_truth("paymentservice", "x"), 10, 10)
        self.assertEqual(epoch_microseconds(window.start) * 1000, epoch_nanoseconds(window.start))
        self.assertEqual(str(epoch_nanoseconds(window.start)), build_query_plan(window)["A"][1]["params"]["start"])

    def test_backend_isolation(self):
        for backend in ("prometheus", "loki", "jaeger"):
            self.assertNotEqual(BACKENDS["A"][backend][:2], BACKENDS["B"][backend][:2])
        self.assertEqual(set(BACKENDS), {"A", "B"})

    def test_scenario_a_logs_are_not_enriched_and_b_correlation_is_preserved(self):
        gt = _ground_truth("paymentservice", "irrelevant")
        window = window_from_ground_truth(gt, 10, 10)
        data_a = normalize_scenario("run-001", "A", window, _raw("A"))
        data_b = normalize_scenario("run-001", "B", window, _raw("B"))
        self.assertIsNone(data_a["logs"][0]["trace_id"])
        self.assertIsNone(data_a["logs"][0]["span_id"])
        self.assertNotIn("trace_id", data_a["logs"][0]["labels"])
        self.assertNotIn("x_tcc_trace_id", data_a["logs"][0]["labels"])
        self.assertNotIn("x_tcc_span_id", data_a["logs"][0]["labels"])
        self.assertEqual(data_b["logs"][0]["trace_id"], "b-trace")
        self.assertEqual(data_b["logs"][0]["span_id"], "b-span")
        self.assertNotIn("fault", str(data_a))
        self.assertNotIn("component", str(data_a["window"]))

    def test_normalization_is_deterministic(self):
        window = window_from_ground_truth(_ground_truth("paymentservice", "x"), 10, 10)
        raw1 = _raw("B")
        raw2 = copy.deepcopy(raw1)
        first = normalize_scenario("run-001", "B", window, raw1)
        second = normalize_scenario("run-001", "B", window, raw2)
        self.assertEqual(first, second)
        self.assertEqual(first["logs"][0]["trace_id"], "b-trace")

    def test_loki_limit_is_paged_without_dropping_entries(self):
        window = window_from_ground_truth(_ground_truth("paymentservice", "x"), 10, 10)
        query = build_query_plan(window)["A"][1]
        first_ns = int(query["params"]["start"])
        last_ns = int(query["params"]["end"])
        entries = [first_ns + 1, last_ns - 1]

        def response_for(_url, params):
            start, end = int(params["start"]), int(params["end"])
            values = [[str(ts), f"entry-{i}"] for i, ts in enumerate(entries) if start <= ts <= end]
            return 200, {"status": "success", "data": {"result": [{"stream": {"service_name": "frontend"}, "values": values}] if values else []}}

        with patch("scripts.experiment.dataset.backends.get_json", side_effect=response_for):
            raw = collect_loki("http://loki", window, "A", query, limit=2)
        self.assertEqual(raw["query"]["records_returned"], 2)
        self.assertEqual(raw["query"]["page_count"], 2)
        self.assertEqual(raw["pages"][0]["data"]["result"][0]["values"] + raw["pages"][1]["data"]["result"][0]["values"],
                         [[str(entries[0]), "entry-0"], [str(entries[1]), "entry-1"]])


if __name__ == "__main__":
    unittest.main()
