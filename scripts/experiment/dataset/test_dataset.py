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
        self.assertIn('pod=~"^(', plan["A"][0]["params"]["query"])
        self.assertIn("redis-cart)-.*$", plan["A"][0]["params"]["query"])
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

    def test_prometheus_evaluation_step_captures_window_edges_for_both_scenarios(self):
        window = window_from_ground_truth(_ground_truth("paymentservice", "x"), 10, 10)
        plan = build_query_plan(window)
        self.assertEqual(plan["A"][0]["params"]["step"], "1s")
        self.assertEqual(plan["B"][0]["params"]["step"], "1s")
        self.assertEqual(plan["A"][0]["params"]["start"], plan["B"][0]["params"]["start"])
        self.assertEqual(plan["A"][0]["params"]["end"], plan["B"][0]["params"]["end"])

    def test_counter_delta_includes_latest_sample_evaluated_at_window_end(self):
        from .contract import common_metrics
        window = {"start": "1970-01-01T00:01:40Z", "end": "1970-01-01T00:01:59Z"}
        series = {"metric": {"__name__": "tcc_conventional_requests_total", "service_name": "paymentservice",
                             "operation": "/hipstershop.PaymentService/Charge", "span_kind": "server", "status_code": "OK"},
                  "values": [[100, "27"], [118, "34"], [119, "40"]]}
        result = common_metrics({"response": {"data": {"result": [series]}}}, "A", window)[0]
        self.assertEqual(result["value"], 13)
        self.assertEqual(result["delta_status"], "valid")

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
        self.assertNotIn("trace_id", data_a["logs"][0]["attributes"])
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

    def test_common_metric_contract_maps_a_b_without_runtime_noise(self):
        from .contract import common_metrics
        window = {"start": "1970-01-01T00:01:40Z", "end": "1970-01-01T00:01:55Z"}
        a_series = []
        for name, values in (
            ("tcc_conventional_requests_total", [[100, "10"], [110, "12"]]),
            ("tcc_conventional_request_duration_seconds_count", [[100, "10"], [110, "12"]]),
            ("tcc_conventional_request_duration_seconds_sum", [[100, "2.0"], [110, "2.6"]]),
        ):
            a_series.append({"metric": {"__name__": name, "service_name": "checkoutservice",
                "operation": "/hipstershop.PaymentService/Charge", "span_kind": "client", "status_code": "OK"}, "values": values})
        a = common_metrics({"response": {"data": {"result": a_series}}}, "A", window)
        b_series = []
        for suffix, values in (("count", [[100, "20"], [110, "22"]]), ("sum", [[100, "4"], [110, "4.6"]])):
            b_series.append({"metric": {"__name__": f"online_boutique_otel_rpc_client_call_duration_seconds_{suffix}",
                "service_name": "checkoutservice", "rpc_method": "hipstershop.PaymentService/Charge",
                "rpc_response_status_code": "OK"}, "values": values})
        b_series.append({"metric": {"__name__": "online_boutique_otel_jvm_gc_duration_seconds_sum",
            "service_name": "checkoutservice"}, "values": [[100, "3"], [110, "4"]]})
        b_series.append({"metric": {"__name__": "container_cpu_usage_seconds_total",
            "service_name": "checkoutservice", "pod": "checkoutservice-abc"}, "values": [[100, "0"], [110, "10"]]})
        b = common_metrics({"response": {"data": {"result": b_series}}}, "B", window)
        self.assertEqual([(x["concept"], x["operation"], x["kind"]) for x in a],
                         [(x["concept"], x["operation"], x["kind"]) for x in b if x["concept"] != "cpu_utilization"])
        for left, right in zip(a, [x for x in b if x["concept"] != "cpu_utilization"]):
            self.assertAlmostEqual(left["value"], right["value"])
        duration_a = next(x for x in a if x["concept"] == "request_duration")
        duration_b = next(x for x in b if x["concept"] == "request_duration")
        self.assertAlmostEqual(duration_a["value"], duration_b["value"])
        self.assertFalse(any("jvm" in name for item in b for name in item.get("native_names", [])))
        cpu = next(x for x in b if x["concept"] == "cpu_utilization")
        self.assertEqual(cpu["unit"], "cpu_cores")
        self.assertEqual(cpu["value"]["avg"], 1.0)

    def test_counter_reset_and_missing_baseline_are_explicit(self):
        from .contract import common_metrics
        window = {"start": "1970-01-01T00:01:40Z", "end": "1970-01-01T00:01:55Z"}
        def series(values):
            return {"metric": {"__name__": "tcc_conventional_requests_total", "service_name": "checkoutservice",
                "operation": "/hipstershop.PaymentService/Charge", "span_kind": "client", "status_code": "OK"}, "values": values}
        reset = common_metrics({"response": {"data": {"result": [series([[100, "10"], [110, "3"]])]}}}, "A", window)[0]
        missing = common_metrics({"response": {"data": {"result": [series([[110, "3"]])]}}}, "A", window)[0]
        self.assertEqual(reset["value"], 3)
        self.assertEqual(reset["delta_status"], "counter_reset")
        self.assertTrue(reset["counter_reset"])
        self.assertIsNone(missing["value"])
        self.assertEqual(missing["delta_status"], "missing_baseline")

        appeared_during_window = common_metrics({"response": {"data": {"result": [series(
            [[110, "5"], [115, "5"]])]}}}, "A", window)[0]
        self.assertIsNone(appeared_during_window["value"])
        self.assertEqual(appeared_during_window["delta_status"], "missing_baseline")

        multi_reset = common_metrics({"response": {"data": {"result": [series(
            [[100, "10"], [105, "12"], [110, "2"], [115, "5"]])]}}}, "A", window)[0]
        self.assertEqual(multi_reset["value"], 7)
        self.assertEqual(multi_reset["delta_status"], "counter_reset")

    def test_common_metric_contract_maps_http_client_cpu_and_redis_errors(self):
        from .contract import common_metrics
        window = {"start": "1970-01-01T00:01:40Z", "end": "1970-01-01T00:01:55Z"}
        cpu = {"__name__": "container_cpu_usage_seconds_total", "service_name": "frontend",
               "pod": "frontend-abc", "container": "server"}
        cpu_values = [[100, "5"], [105, "10"], [110, "12"]]
        a = common_metrics({"response": {"data": {"result": [{"metric": cpu, "values": cpu_values}]}}}, "A", window)
        b = common_metrics({"response": {"data": {"result": [{"metric": cpu, "values": cpu_values}]}}}, "B", window)
        self.assertEqual(a, b)
        self.assertEqual(a[0]["concept"], "cpu_utilization")
        self.assertEqual(a[0]["value"], {"first": 1.0, "min": 0.4, "max": 1.0, "avg": 0.7, "last": 0.4})

        b_series = []
        for suffix, values in (("count", [[100, "0"], [110, "3"]]), ("sum", [[100, "0"], [110, "1.5"]])):
            b_series.append({"metric": {"__name__": f"online_boutique_otel_tcc_otel_rpc_client_duration_seconds_{suffix}",
                "service_name": "recommendationservice", "rpc_method": "hipstershop.ProductCatalogService/ListProducts",
                "rpc_response_status_code": "OK"}, "values": values})
        b_series.append({"metric": {"__name__": "online_boutique_otel_tcc_otel_rpc_client_requests_total",
            "service_name": "recommendationservice", "rpc_method": "hipstershop.ProductCatalogService/ListProducts",
            "rpc_response_status_code": "OK"}, "values": [[100, "0"], [110, "3"]]})
        b_series.append({"metric": {"__name__": "online_boutique_otel_tcc_otel_http_server_requests_total",
            "service_name": "frontend", "http_request_method": "GET", "http_route": "/cart",
            "http_response_status_code": "200"}, "values": [[100, "0"], [110, "2"]]})
        b_series.append({"metric": {"__name__": "online_boutique_otel_cart_redis_operations_total",
            "service_name": "cartservice", "db_operation_name": "get", "outcome": "error"},
            "values": [[100, "0"], [110, "1"]]})
        b_series.append({"metric": {"__name__": "online_boutique_otel_tcc_otel_rpc_client_duration_seconds_bucket",
            "service_name": "recommendationservice", "rpc_method": "hipstershop.ProductCatalogService/ListProducts",
            "le": "+Inf"}, "values": [[100, "2"], [110, "5"]]})
        output = common_metrics({"response": {"data": {"result": b_series}}}, "B", window)
        concepts = {(row["service"], row["operation"], row["kind"], row["outcome"], row["concept"])
                    for row in output if row["concept"] != "cpu_utilization"}
        self.assertIn(("recommendationservice", "ProductCatalogService/ListProducts", "client", "success", "request_count"), concepts)
        self.assertIn(("frontend", "GET /cart", "server", "success", "request_count"), concepts)
        self.assertIn(("cartservice", "redis GET", "datastore", "error", "request_count"), concepts)
        self.assertFalse(any("bucket" in name for row in output for name in row["native_names"]))

    def test_http_route_with_leading_slash_is_canonicalized(self):
        from .contract import _canonical_operation
        labels = {"http_request_method": "GET", "http_route": "/cart"}
        self.assertEqual(_canonical_operation(labels, "tcc_otel_http_server_requests_total"), "GET /cart")

    def test_log_view_preserves_only_native_correlation_and_compacts_resources(self):
        from .contract import common_logs
        raw_a = {"response": {"data": {"result": [{"stream": {"service_name": "paymentservice", "telemetry_scenario": "conventional", "filename": "/pod/x"}, "values": [["1793164800000000000", '{"severity":"ERROR","message":"Charge failed"}']]}]}}}
        raw_b = {"response": {"data": {"result": [{"stream": {"service_name": "paymentservice", "telemetry_sdk_name": "opentelemetry", "trace_id": "abc", "span_id": "def", "scope_name": "native-scope"}, "values": [["1793164800000000000", 'Charge failed']]}]}}}
        a = common_logs(raw_a, "A")[0]
        b = common_logs(raw_b, "B")[0]
        self.assertIsNone(a["trace_id"]); self.assertIsNone(a["span_id"])
        self.assertEqual(b["trace_id"], "abc"); self.assertEqual(b["span_id"], "def")
        self.assertNotIn("telemetry_sdk_name", b["attributes"])
        self.assertEqual(raw_b["response"]["data"]["result"][0]["stream"]["telemetry_sdk_name"], "opentelemetry")

    def test_log_contract_compacts_structured_logger_envelopes_and_dynamic_ids(self):
        from .contract import common_logs
        a_raw = {"response": {"data": {"result": [{"stream": {"service_name": "cartservice"}, "values": [[
            "1793164800000000000", json.dumps({"timestamp": "t", "pid": 1, "name": "cart", "level": "Information",
                "message": "AddItemAsync called with userId=abc-123, productId=OLJCESPC7Z, quantity=1"})]]}]}}}
        b_raw = {"response": {"data": {"result": [{"stream": {"service_name": "cartservice", "trace_id": "t1", "span_id": "s1"}, "values": [[
            "1793164800000000000", "AddItemAsync called with userId={UserId}, productId={ProductId}, quantity={Quantity}"]]}]}}}
        a_original = copy.deepcopy(a_raw)
        a = common_logs(a_raw, "A")[0]
        b = common_logs(b_raw, "B")[0]
        self.assertEqual(a["body"], b["body"])
        self.assertEqual(a["body"], "AddItemAsync called with userId={UserId}, productId={ProductId}, quantity={Quantity}")
        self.assertEqual(a["severity"], "INFO")
        self.assertEqual(a["attributes"], {})
        self.assertEqual((b["trace_id"], b["span_id"]), ("t1", "s1"))
        self.assertEqual(a_raw, a_original)

    def test_conventional_dotnet_console_envelope_matches_structured_otel_event(self):
        from .contract import common_logs
        timestamp = "1793164800000000000"
        conventional = {"response": {"data": {"result": [{"stream": {"service_name": "cartservice"}, "values": [[
            timestamp, "info: cartservice.services.CartService[0]\n      Reading cart for user 123e4567-e89b-12d3-a456-426614174000"]]}]}}}
        otel = {"response": {"data": {"result": [{"stream": {"service_name": "cartservice", "severity": "Information",
            "trace_id": "abc", "span_id": "def"}, "values": [[timestamp, "Reading cart for user {UserId}"]]}]}}}
        a = common_logs(conventional, "A")[0]
        b = common_logs(otel, "B")[0]
        self.assertEqual(a["severity"], "INFO")
        self.assertEqual(b["severity"], "INFO")
        self.assertEqual(a["body"], b["body"])
        self.assertEqual(a["body"], "Reading cart for user {UserId}")
        self.assertIsNone(a["trace_id"])
        self.assertEqual(b["trace_id"], "abc")

    def test_dotnet_error_details_are_common_without_stacktrace(self):
        from .contract import common_logs
        timestamp = "1793164800000000000"
        detail = "Can't access cart storage. StackExchange.Redis.RedisConnectionException: SocketFailure\n ---> System.Exception: nested\n   at Redis.Read()"
        a = {"response": {"data": {"result": [{"stream": {"service_name": "cartservice"}, "values": [[
            timestamp, f"Error status code 'FailedPrecondition' with detail '{detail}' raised."]]}]}}}
        b = {"response": {"data": {"result": [{"stream": {"service_name": "cartservice", "StatusCode": "FailedPrecondition",
            "Detail": detail, "severity_text": "Error", "trace_id": "t", "span_id": "s"}, "values": [[
                timestamp, "Error status code '{StatusCode}' with detail '{Detail}' raised."]]}]}}}
        log_a = common_logs(a, "A")[0]
        log_b = common_logs(b, "B")[0]
        self.assertEqual(log_a["body"], log_b["body"])
        self.assertEqual(log_a["attributes"], log_b["attributes"])
        self.assertEqual(log_a["attributes"]["error.type"], "StackExchange.Redis.RedisConnectionException")
        self.assertNotIn("nested", str(log_b))
        self.assertIsNone(log_a["trace_id"])
        self.assertEqual(log_b["trace_id"], "t")

    def test_common_trace_view_drops_health_scrape_noise_and_preserves_datastore_correlation(self):
        from .contract import common_traces
        raw = {"search_results": [{"response": {"data": [{"traceID": "trace-x", "processes": {"p": {"serviceName": "cartservice"}},
            "spans": [
                {"spanID": "s1", "processID": "p", "operationName": "redis GET", "startTime": 1793164800000000, "duration": 2000,
                 "references": [], "tags": [{"key": "span.kind", "value": "client"}, {"key": "db.system.name", "value": "redis"}]},
                {"spanID": "s2", "processID": "p", "operationName": "grpc.health.v1.Health/Check", "startTime": 1793164800000000, "duration": 100,
                 "references": [], "tags": [{"key": "span.kind", "value": "server"}]},
            ]}]}}]}
        traces = common_traces(raw)
        self.assertEqual(len(traces), 1)
        self.assertEqual(len(traces[0]["spans"]), 1)
        self.assertEqual(traces[0]["spans"][0]["kind"], "datastore")
        self.assertEqual(traces[0]["spans"][0]["target"], "redis-cart")

    def test_jaeger_process_ids_are_scoped_to_each_search_payload(self):
        from .contract import common_traces
        raw = {"search_results": [
            {"response": {"data": [{"traceID": "trace-x", "processes": {"p1": {"serviceName": "cartservice"}},
                "spans": [{"spanID": "cart-span", "processID": "p1", "operationName": "/hipstershop.CartService/GetCart",
                    "startTime": 1793164800000000, "duration": 1200, "references": [],
                    "tags": [{"key": "span.kind", "value": "server"}]}]}]}},
            {"response": {"data": [{"traceID": "trace-x", "processes": {"p1": {"serviceName": "checkoutservice"}},
                "spans": [{"spanID": "checkout-span", "processID": "p1", "operationName": "/hipstershop.CheckoutService/PlaceOrder",
                    "startTime": 1793164800001000, "duration": 900, "references": [],
                    "tags": [{"key": "span.kind", "value": "server"}]}]}]}},
        ]}
        traces = common_traces(raw)
        self.assertEqual(len(traces), 1)
        services = {span["span_id"]: span["service"] for span in traces[0]["spans"]}
        self.assertEqual(services, {"cart-span": "cartservice", "checkout-span": "checkoutservice"})

    def test_metric_contract_is_independent_of_fault_ground_truth(self):
        from .contract import common_metrics
        window = {"start": "1970-01-01T00:01:40Z", "end": "1970-01-01T00:01:50Z"}
        raw = {"response": {"data": {"result": [{"metric": {"__name__": "tcc_conventional_requests_total",
            "service_name": "checkoutservice", "operation": "/hipstershop.PaymentService/Charge", "span_kind": "client"},
            "values": [[100, "1"], [110, "2"]]}]}}}
        # The transform has no Ground Truth parameter, so changing fault metadata
        # cannot affect its selected evidence.
        self.assertEqual(common_metrics(raw, "A", window), common_metrics(raw, "A", window))

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
