from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .backends import BACKENDS, collect_loki
from .builder import normalize_run
from .core import (DatasetError, SUT_SERVICES, build_query_plan, epoch_microseconds,
                   epoch_nanoseconds, sha256_file, window_from_ground_truth, write_json_atomic)
from .normalize import normalize_scenario


def _ground_truth(component: str, fault_type: str) -> dict:
    return {
        "status": "valid", "validation": {"injection_confirmed": True, "recovery_confirmed": True},
        "fault": {"class": "F4", "type": fault_type, "location": {"component": component}, "parameters": {"secret": "must-not-leak"}},
        "timing": {"confirmed_start": "2026-10-28T05:19:54.631Z", "confirmed_end": "2026-10-28T05:20:03.996Z"},
    }


def _raw(scenario: str) -> dict:
    labels = {"service_name": "checkoutservice", "severity": "INFO"}
    if scenario == "B":
        labels.update({"trace_id": "b-trace", "span_id": "b-span"})
    return {
        "metrics": {"response": {"data": {"resultType": "matrix", "result": [
            {"metric": {"__name__": "requests_total", "service_name": "checkoutservice", "method": "Checkout"},
             "values": [[1793164800, "2"]]}]}}, "metadata": {}},
        "logs": {"response": {"data": {"result": [{
            "stream": labels,
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

    def test_scenario_a_without_correlation_is_valid_and_b_correlation_is_preserved(self):
        gt = _ground_truth("paymentservice", "irrelevant")
        window = window_from_ground_truth(gt, 10, 10)
        raw_a = _raw("A")
        original_a = copy.deepcopy(raw_a)
        data_a = normalize_scenario("run-001", "A", window, raw_a)
        data_b = normalize_scenario("run-001", "B", window, _raw("B"))
        self.assertEqual(raw_a, original_a)
        self.assertIsNone(data_a["logs"][0]["trace_id"])
        self.assertIsNone(data_a["logs"][0]["span_id"])
        self.assertNotIn("trace_id", data_a["logs"][0]["labels"])
        self.assertEqual(data_b["logs"][0]["trace_id"], "b-trace")
        self.assertEqual(data_b["logs"][0]["span_id"], "b-span")
        self.assertNotIn("fault", str(data_a))
        self.assertNotIn("component", str(data_a["window"]))

    def test_scenario_a_trace_id_is_rejected_without_mutating_raw(self):
        raw = _raw("A")
        raw["logs"]["response"]["data"]["result"][0]["stream"]["trace_id"] = "unexpected"
        original = copy.deepcopy(raw)
        window = window_from_ground_truth(_ground_truth("paymentservice", "x"), 10, 10)
        with self.assertRaisesRegex(DatasetError, "Scenario A RAW contains explicit trace/span"):
            normalize_scenario("run-001", "A", window, raw)
        self.assertEqual(raw, original)

    def test_scenario_a_span_id_is_rejected(self):
        raw = _raw("A")
        raw["logs"]["response"]["data"]["result"][0]["stream"]["span_id"] = "unexpected"
        window = window_from_ground_truth(_ground_truth("paymentservice", "x"), 10, 10)
        with self.assertRaisesRegex(DatasetError, "span_id"):
            normalize_scenario("run-001", "A", window, raw)

    def test_scenario_a_rejects_loki_correlation_key_variants(self):
        variants = (
            "trace_id", "span_id", "trace.id", "span.id", "traceId", "spanId", "traceID", "spanID",
            "x_tcc_trace_id", "x_tcc_span_id", "x-tcc-trace-id", "x-tcc-span-id", "traceparent",
            "trace_state", "x-b3-traceid", "x-b3-spanid", "uber-trace-id",
        )
        window = window_from_ground_truth(_ground_truth("paymentservice", "x"), 10, 10)
        for field in variants:
            with self.subTest(field=field):
                raw = _raw("A")
                raw["logs"]["response"]["data"]["result"][0]["stream"][field] = "unexpected"
                with self.assertRaises(DatasetError):
                    normalize_scenario("run-001", "A", window, raw)

    def test_scenario_a_checks_structured_metadata_and_structured_log_bodies(self):
        window = window_from_ground_truth(_ground_truth("paymentservice", "x"), 10, 10)
        structured = _raw("A")
        structured_stream = structured["logs"]["response"]["data"]["result"][0]
        structured_stream["structuredMetadata"] = {"trace.id": "unexpected"}
        body = _raw("A")
        body_stream = body["logs"]["response"]["data"]["result"][0]
        body_stream["values"][0][1] = '{"message":"x","spanId":"unexpected"}'
        logfmt = _raw("A")
        logfmt_stream = logfmt["logs"]["response"]["data"]["result"][0]
        logfmt_stream["values"][0][1] = "request finished trace_id=unexpected"
        paged_raw = _raw("A")
        paged_raw["pages"] = [{"data": {"result": [{"stream": {"service_name": "checkoutservice", "span.id": "unexpected"}}]}}]
        for raw in (structured, body, logfmt, paged_raw):
            with self.subTest(raw=raw):
                with self.assertRaises(DatasetError):
                    normalize_scenario("run-001", "A", window, raw)

    def test_detected_scenario_a_correlation_marks_build_invalid_normalization(self):
        run_id = "run-a-correlation-test"
        with tempfile.TemporaryDirectory() as temp_dir:
            dataset_root = Path(temp_dir) / "datasets"
            run_root = dataset_root / run_id
            manifest = {
                "schema_version": "1.0", "run_id": run_id, "status": "collected",
                "window": {"confirmed_start": "2026-10-28T05:19:54.631Z", "confirmed_end": "2026-10-28T05:20:03.996Z",
                           "start": "2026-10-28T05:19:44.631Z", "end": "2026-10-28T05:20:13.996Z",
                           "pre_buffer_seconds": 10, "post_buffer_seconds": 10},
                "scenarios": {s: {"status": "collected", "sources": {}} for s in ("A", "B")},
                "raw_sha256": {},
            }
            for scenario in ("A", "B"):
                signals = _raw(scenario)
                if scenario == "A":
                    signals["logs"]["response"]["data"]["result"][0]["stream"]["trace_id"] = "contamination"
                raw_dir = run_root / f"scenario-{scenario.lower()}" / "raw"
                for signal, document in signals.items():
                    raw_path = raw_dir / f"{signal}.json"
                    write_json_atomic(raw_path, document)
                    relative = f"scenario-{scenario.lower()}/raw/{signal}.json"
                    manifest["raw_sha256"][relative] = sha256_file(raw_path)
            write_json_atomic(run_root / "manifest.json", manifest)
            raw_a_path = run_root / "scenario-a/raw/logs.json"
            raw_a_hash = sha256_file(raw_a_path)
            with patch("scripts.experiment.dataset.builder.DATASET_ROOT", dataset_root):
                with self.assertRaisesRegex(DatasetError, "Scenario A RAW contains explicit trace/span"):
                    normalize_run(run_id)
            failed_manifest = json.loads((run_root / "manifest.json").read_text())
            self.assertEqual(failed_manifest["status"], "invalid_normalization")
            self.assertEqual(sha256_file(raw_a_path), raw_a_hash)

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
